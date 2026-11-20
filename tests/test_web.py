"""端到端 Web 流程测试：上传 → 勾选/剔除 → 预览 → 下载 / 冲突阻止。"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path / "data"))
    main.SESSIONS.clear()
    return TestClient(main.app)


def _upload(client, sample_db):
    with open(sample_db, "rb") as f:
        resp = client.post(
            "/upload",
            files={"file": ("lab.db", f, "application/x-sqlite3")},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/select"


def test_full_flow_download(client, sample_db, tmp_path):
    _upload(client, sample_db)

    resp = client.get("/select")
    assert resp.status_code == 200
    assert "墙体试件A" in resp.text and "背景噪声对照测量" in resp.text

    # R102→R901 频点冲突（125Hz vs 100Hz）、R103 无对照链，
    # 质量核查新规则下需剔除这两条后才允许确认
    resp = client.post(
        "/select",
        data={"test": ["1"], "excl": ["102", "103"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/preview")
    assert resp.status_code == 200
    assert "确认报告并下载" in resp.text
    assert "901" in resp.text  # 跨组带入的对照读数出现在预览中

    resp = client.post("/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-sqlite3")
    out = tmp_path / "downloaded.db"
    out.write_bytes(resp.content)
    conn = sqlite3.connect(out)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reading_ids = {r[0] for r in conn.execute("SELECT id FROM readings")}
    assert reading_ids == {101, 901}
    conn.close()


def test_conflict_blocks_download(client, sample_db):
    _upload(client, sample_db)
    # 勾选 T1 与 T9，并剔除被 T1 读数引用的对照读数 R901
    resp = client.post(
        "/select",
        data={"test": ["1", "9"], "excl": ["901"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get("/preview")
    assert resp.status_code == 200
    assert "已阻止下载" in resp.text
    assert "剔除" in resp.text  # 引用链与冲突原因展示在预览页

    resp = client.post("/download")
    assert resp.status_code == 409
    assert "阻止" in resp.text


def test_exclusion_of_unselected_test_reading_ignored(client, sample_db):
    """剔除未勾选试验的读数被静默忽略，不影响交付。

    同时剔除所选 T1 中存在质量问题的 R102（频点冲突）与 R103（无对照链），
    使质量核查通过，验证下载不受未勾选试验剔除项的影响。
    """
    _upload(client, sample_db)
    client.post(
        "/select",
        data={"test": ["1"], "excl": ["901", "102", "103"]},
    )
    resp = client.get("/preview")
    assert "确认报告并下载" in resp.text
    resp = client.post("/download")
    assert resp.status_code == 200


def test_upload_rejects_bad_file(client, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not sqlite")
    with open(bad, "rb") as f:
        resp = client.post("/upload", files={"file": ("bad.db", f)})
    assert resp.status_code == 400
    assert "SQLite" in resp.text


def test_upload_rejects_wrong_schema(client, tmp_path):
    wrong = tmp_path / "wrong.db"
    conn = sqlite3.connect(wrong)
    conn.execute("CREATE TABLE whatever (id INTEGER)")
    conn.commit()
    conn.close()
    with open(wrong, "rb") as f:
        resp = client.post("/upload", files={"file": ("wrong.db", f)})
    assert resp.status_code == 400
    assert "缺少数据表" in resp.text


def test_pages_require_session(client):
    assert client.get("/select", follow_redirects=False).status_code == 303
    assert client.get("/preview", follow_redirects=False).status_code == 303
    assert client.get("/quality", follow_redirects=False).status_code == 303


def test_quality_page_lists_conflicts_grouped_by_test(client, sample_db):
    """质量核查页展示频点冲突链，并按受影响试验汇总。"""
    _upload(client, sample_db)
    client.post("/select", data={"test": ["1", "3"]})
    resp = client.get("/quality")
    assert resp.status_code == 200
    assert "引用质量核查" in resp.text
    assert "频点冲突" in resp.text
    # 按受影响试验汇总：T1（R102）与 T3（R201）都受影响
    assert "按受影响试验汇总" in resp.text
    # 完整引用链中的读数号可见
    assert "102" in resp.text and "901" in resp.text
    # 存在问题时确认按钮禁用
    assert "disabled" in resp.text


def test_quality_failure_blocks_download(client, sample_db):
    """质量核查未通过时，即使引用解析无冲突也拒绝确认（409）。"""
    _upload(client, sample_db)
    # 只选 T1 并剔除 R103（无对照），但保留频点冲突的 R102
    client.post("/select", data={"test": ["1"], "excl": ["103"]})
    resp = client.get("/quality")
    assert "频点冲突" in resp.text
    resp = client.post("/download")
    assert resp.status_code == 409
    assert "质量核查" in resp.text


def test_quality_pass_allows_download(client, sample_db):
    """剔除问题读数后核查通过，确认成功。"""
    _upload(client, sample_db)
    client.post("/select", data={"test": ["1"], "excl": ["102", "103"]})
    resp = client.get("/quality")
    assert "全部通过" in resp.text
    resp = client.post("/download")
    assert resp.status_code == 200


def test_source_replacement_blocks_confirmation_and_keeps_last_result(
    client, sample_db
):
    """源库被更换：拒绝确认（409），上一版子库仍可下载。"""
    _upload(client, sample_db)
    client.post("/select", data={"test": ["1"], "excl": ["102", "103"]})
    # 第一次确认成功，保存上一版结果
    resp = client.post("/download")
    assert resp.status_code == 200
    first = resp.content
    assert len(first) > 0

    # 直接篡改工作目录中的源库文件，模拟源库更换
    sid = client.cookies.get("sid")
    state = main.SESSIONS[sid]
    with open(state.db_path, "wb") as f:
        f.write(b"replaced source db contents")

    # 重新确认被拒绝
    resp = client.post("/download")
    assert resp.status_code == 409
    assert "更换" in resp.text
    # 质量页与预览页给出阻止/更换告警（损坏时降级为阻止页）
    assert client.get("/quality").status_code == 409
    assert client.get("/preview").status_code == 409
    # 上一版结果保留且可取回，内容与首次交付一致
    resp = client.get("/last-subset")
    assert resp.status_code == 200
    assert resp.content == first


def test_identical_replacement_does_not_block(client, sample_db, tmp_path):
    """内容未变（同内容覆盖）不视为源库更换，仍可确认。"""
    _upload(client, sample_db)
    client.post("/select", data={"test": ["1"], "excl": ["102", "103"]})
    sid = client.cookies.get("sid")
    state = main.SESSIONS[sid]
    with open(sample_db, "rb") as f:
        original = f.read()
    with open(state.db_path, "wb") as f:
        f.write(original)  # 同内容重写
    resp = client.post("/download")
    assert resp.status_code == 200


def test_valid_but_different_source_shows_banner_and_blocks(client, sample_db, tmp_path):
    """换成另一个有效的 SQLite 库：页面显示更换横幅且确认被拒。"""
    from app.sampledb import create_sample_db

    _upload(client, sample_db)
    client.post("/select", data={"test": ["1"], "excl": ["102", "103"]})
    assert client.post("/download").status_code == 200

    # 生成另一个有效库并改动内容（指纹不同）覆盖源库
    other = tmp_path / "other.db"
    create_sample_db(str(other))
    conn = sqlite3.connect(other)
    conn.execute("UPDATE readings SET level_db = level_db + 1 WHERE id = 101")
    conn.commit()
    conn.close()
    sid = client.cookies.get("sid")
    state = main.SESSIONS[sid]
    with open(other, "rb") as f:
        state_bytes = f.read()
    with open(state.db_path, "wb") as f:
        f.write(state_bytes)

    q = client.get("/quality")
    assert q.status_code == 200
    assert "更换" in q.text
    p = client.get("/preview")
    assert "更换" in p.text
    assert client.post("/download").status_code == 409
    # 上一版结果仍可取回
    assert client.get("/last-subset").status_code == 200
