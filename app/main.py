"""隔声试验数据交付复核 Web 应用（FastAPI）。

流程：上传 SQLite 库 → 勾选试验 / 剔除异常读数 → 预览引用解析结果
（含跨组拉入链与冲突）→ 引用质量核查（对照链频点一致性）→
确认后下载 SQLite 子库。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .core import build_subset, load_model, validate_schema
from .quality import check_quality_for_selection

DATA_DIR = os.environ.get("DATA_DIR", "./data")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))

app = FastAPI(title="隔声试验数据交付复核")
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
)


@dataclass
class SessionState:
    workdir: str
    source_name: str
    source_sha256: str = ""
    selected_tests: set[int] = field(default_factory=set)
    excluded_readings: set[int] = field(default_factory=set)
    # 最近一次成功确认生成的子库，用于源库更换后保留上一版结果
    last_subset_path: str | None = None
    last_subset_name: str | None = None

    @property
    def db_path(self) -> str:
        return os.path.join(self.workdir, "source.db")


# 单进程内存会话；重启即失效（上传文件保留在 DATA_DIR 下，由运维清理）
SESSIONS: dict[str, SessionState] = {}


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_session(request: Request) -> SessionState | None:
    sid = request.cookies.get("sid")
    return SESSIONS.get(sid) if sid else None


def _load(st: SessionState):
    conn = sqlite3.connect(f"file:{st.db_path}?mode=ro", uri=True)
    try:
        return load_model(conn)
    finally:
        conn.close()


def _try_load(st: SessionState):
    """加载源库；文件已损坏/被替换为非 SQLite 时返回 None 而非抛异常。"""
    try:
        return _load(st)
    except sqlite3.DatabaseError:
        return None


def _source_changed(st: SessionState) -> bool:
    """源库已更换/被篡改：当前文件指纹与上传时记录的不一致。"""
    try:
        return _sha256_file(st.db_path) != st.source_sha256
    except OSError:
        return True


def _blocked_page(message: str, st: SessionState | None = None) -> HTMLResponse:
    keep = ""
    if st and st.last_subset_path and os.path.exists(st.last_subset_path):
        keep = (
            "<p>已保留上一版交付结果，可继续下载："
            f"<a href='/last-subset'>{st.last_subset_name}</a></p>"
        )
    return HTMLResponse(
        "<h2>操作已阻止</h2>"
        f"<p>{message}</p>"
        f"{keep}"
        "<p>请返回 <a href='/preview'>预览</a> 或 "
        "<a href='/quality'>引用质量核查</a> 查看详情，"
        "或 <a href='/'>重新上传</a>。</p>",
        status_code=409,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "upload.html", {"error": None}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile):
    error: str | None = None
    if not file.filename:
        error = "未选择文件"
    data = await file.read()
    if error is None and len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        error = f"文件超过大小限制（{MAX_UPLOAD_MB} MB）"

    if error is None:
        # 旧会话的工作目录一并清理
        old = _get_session(request)
        if old:
            SESSIONS.pop(request.cookies.get("sid"), None)
            shutil.rmtree(old.workdir, ignore_errors=True)

        sid = uuid.uuid4().hex
        workdir = os.path.join(DATA_DIR, sid)
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, "source.db")
        with open(path, "wb") as f:
            f.write(data)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            errors = validate_schema(conn)
            conn.close()
        except sqlite3.DatabaseError:
            errors = ["文件不是有效的 SQLite 数据库"]
        if errors:
            shutil.rmtree(workdir, ignore_errors=True)
            return templates.TemplateResponse(
                request,
                "upload.html",
                {"error": "；".join(errors)},
                status_code=400,
            )
        SESSIONS[sid] = SessionState(
            workdir,
            file.filename or "source.db",
            source_sha256=_sha256_file(path),
        )
        resp = RedirectResponse("/select", status_code=303)
        resp.set_cookie("sid", sid, httponly=True)
        return resp

    return templates.TemplateResponse(
        request, "upload.html", {"error": error}, status_code=400
    )


# 读数表展示时的优先列序，其余列按出现顺序排后
_PREFERRED_READING_COLS = [
    "id", "kind", "frequency_hz", "level_db",
    "control_reading_id", "calibration_id", "note",
]


@app.get("/select", response_class=HTMLResponse)
def select_page(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    model = _try_load(st)
    if model is None:
        return templates.TemplateResponse(
            request,
            "blocked.html",
            {
                "message": "源库文件已无法读取（被更换为非 SQLite 内容或已损坏）。",
                "st": st,
            },
            status_code=409,
        )

    headers: list[str] = []
    for r in model.readings.values():
        for col in r:
            if col not in headers:
                headers.append(col)
    headers = [c for c in _PREFERRED_READING_COLS if c in headers] + [
        c for c in headers if c not in _PREFERRED_READING_COLS
    ]

    groups = []
    for gid in sorted(model.groups):
        g = model.groups[gid]
        tests = []
        for t in sorted(model.tests.values(), key=lambda x: x["id"]):
            if t["group_id"] != gid:
                continue
            readings = [
                {
                    "id": r["id"],
                    "cells": [r.get(c) for c in headers],
                    "excluded": r["id"] in st.excluded_readings,
                }
                for r in (
                    model.readings[rid]
                    for rid in model.readings_by_test.get(t["id"], [])
                )
            ]
            tests.append(
                {"row": t, "readings": readings, "selected": t["id"] in st.selected_tests}
            )
        groups.append({"row": g, "tests": tests})

    return templates.TemplateResponse(
        request,
        "select.html",
        {
            "groups": groups,
            "headers": headers,
            "source": st.source_name,
        },
    )


@app.post("/select")
async def select_save(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    model = _try_load(st)
    if model is None:
        return templates.TemplateResponse(
            request,
            "blocked.html",
            {
                "message": "源库文件已无法读取（被更换为非 SQLite 内容或已损坏），选择未保存。",
                "st": st,
            },
            status_code=409,
        )

    selected = {int(v) for v in form.getlist("test")} & set(model.tests)
    # 剔除仅对所选试验内的读数生效，其余静默忽略
    excluded = {
        int(v)
        for v in form.getlist("excl")
        if (r := model.readings.get(int(v))) and r["test_id"] in selected
    }
    st.selected_tests = selected
    st.excluded_readings = excluded
    return RedirectResponse("/preview", status_code=303)


@app.get("/preview", response_class=HTMLResponse)
def preview(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    source_changed = _source_changed(st)
    model = _try_load(st)
    if model is None:
        return templates.TemplateResponse(
            request,
            "blocked.html",
            {
                "message": "源库文件已无法读取（被更换为非 SQLite 内容或已损坏）。",
                "st": st,
            },
            status_code=409,
        )
    res, quality = check_quality_for_selection(
        model, st.selected_tests, st.excluded_readings
    )
    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "res": res,
            "model": model,
            "quality": quality,
            "selected": sorted(st.selected_tests),
            "excluded": sorted(st.excluded_readings),
            "pulled_in": sorted(res.pulled_in.items()),
            "source": st.source_name,
            "source_changed": source_changed,
        },
    )


def _test_heading(model, tid: int) -> str:
    t = model.tests.get(tid)
    if t is None:
        return f"试验 #{tid}（不存在）"
    g = model.groups.get(t.get("group_id"), {})
    return (
        f"试验 #{tid}「{t.get('name', '?')}」"
        f"（组 #{g.get('id', '?')}「{g.get('name', '?')}」）"
    )


@app.get("/quality", response_class=HTMLResponse)
def quality_page(request: Request):
    """引用质量核查入口：对照链频点一致性，按受影响试验汇总。"""
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)
    source_changed = _source_changed(st)
    model = _try_load(st)
    if model is None:
        return templates.TemplateResponse(
            request,
            "blocked.html",
            {
                "message": "源库文件已无法读取（被更换为非 SQLite 内容或已损坏），无法执行质量核查。",
                "st": st,
            },
            status_code=409,
        )
    res, quality = check_quality_for_selection(
        model, st.selected_tests, st.excluded_readings
    )
    groups = [
        {"test_id": tid, "heading": _test_heading(model, tid), "issues": issues}
        for tid, issues in quality.by_test.items()
    ]
    return templates.TemplateResponse(
        request,
        "quality.html",
        {
            "res": res,
            "quality": quality,
            "groups": groups,
            "selected": sorted(st.selected_tests),
            "source": st.source_name,
            "source_changed": source_changed,
        },
    )


@app.get("/last-subset")
def last_subset(request: Request):
    """源库更换后仍可下载上一版已确认的子库结果。"""
    st = _get_session(request)
    if not st or not st.last_subset_path or not os.path.exists(st.last_subset_path):
        return RedirectResponse("/", status_code=303)
    return FileResponse(
        st.last_subset_path,
        filename=st.last_subset_name or "subset.db",
        media_type="application/x-sqlite3",
    )


@app.post("/download")
def download(request: Request):
    st = _get_session(request)
    if not st:
        return RedirectResponse("/", status_code=303)

    # 源库已更换/被篡改：拒绝重新确认，保留上一版结果
    if _source_changed(st):
        return _blocked_page(
            "源库文件自上传后已被更换或修改，基于当前文件的确认已被拒绝。",
            st,
        )

    model = _try_load(st)
    if model is None:
        return _blocked_page(
            "源库文件已无法读取（被更换为非 SQLite 内容或已损坏），确认已被拒绝。",
            st,
        )
    # 下载前重新解析与核查，防止预览之后状态被修改导致不一致
    res, quality = check_quality_for_selection(
        model, st.selected_tests, st.excluded_readings
    )
    if not res.ok:
        return _blocked_page(
            "存在未解决的引用冲突（悬空或指向已剔除读数），"
            "请返回 <a href='/preview'>预览</a> 查看引用链，"
            "或回 <a href='/select'>选择页</a> 修改。"
        )
    if not quality.ok:
        return _blocked_page(
            f"引用质量核查未通过：{len(quality.issues)} 条对照链存在问题"
            "（频点冲突 / 缺频点 / 悬空 / 成环 / 链末端非对照），"
            "请返回 <a href='/quality'>引用质量核查</a> 查看完整引用链。"
        )

    out_path = os.path.join(st.workdir, f"subset_{int(time.time() * 1000)}.db")
    build_subset(st.db_path, out_path, res)
    # 记录为上一版结果；源库日后被更换时仍可取回
    st.last_subset_path = out_path
    st.last_subset_name = os.path.splitext(st.source_name)[0] + "_subset.db"
    return FileResponse(
        out_path, filename=st.last_subset_name, media_type="application/x-sqlite3"
    )
