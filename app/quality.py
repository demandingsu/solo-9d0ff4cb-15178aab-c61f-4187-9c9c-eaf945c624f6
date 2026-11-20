"""对照链引用质量核查。

声学实验员怀疑对照链把不同频点的读数混在一起。本模块对交付范围内的
measurement（测量）读数，沿 ``control_reading_id`` 逐跳核查：

1. 链上相邻读数的 ``frequency_hz`` 必须相同（频点冲突即记为问题）；
2. 任一读数缺 ``frequency_hz``（NULL）记为缺频点，停止继续追链；
3. ``control_reading_id`` 指向不存在的读数记为悬空；
4. 链上出现已访问读数记为成环；
5. 链的末端读数必须是 control（对照）读数，否则记为链末端非对照。

每个 measurement 读数最多报告沿链发现的*首个*问题，并附带从该读数出发的
完整引用链（含跨组引用）；问题按受影响试验（起始 measurement 所属试验）
汇总。全部 measurement 的对照链均通过时，报告才允许确认。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .core import Hop, Model, Resolution, resolve

# 问题类型（reason 文案同时直接用于页面展示与测试断言）
REASON_FREQUENCY_CONFLICT = "频点冲突：对照链上相邻读数的 frequency_hz 不一致"
REASON_MISSING_FREQUENCY = "缺频点：读数的 frequency_hz 为空"
REASON_DANGLING = "悬空：引用的对照读数不存在"
REASON_CYCLE = "成环：对照链回到了链上已出现的读数"
REASON_BAD_TERMINAL = "链末端非对照：对照链终点不是 control 读数"
REASON_NO_CONTROL = "无对照：measurement 读数没有 control_reading_id"

MEASUREMENT_KIND = "measurement"
CONTROL_KIND = "control"


@dataclass
class QualityIssue:
    """一条未通过核查的对照链。chain 为从起始 measurement 出发的完整链。"""

    test_id: int
    reading_id: int
    reason: str
    chain: list[Hop]


@dataclass
class QualityReport:
    """一次质量核查的结果：起始 measurement 总数、问题列表及按试验汇总。"""

    checked: int = 0
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def by_test(self) -> dict[int, list[QualityIssue]]:
        grouped: dict[int, list[QualityIssue]] = {}
        for issue in self.issues:
            grouped.setdefault(issue.test_id, []).append(issue)
        return dict(sorted(grouped.items()))


def _freq_label(value) -> str:
    if value is None:
        return "频率=空"
    if isinstance(value, float) and value.is_integer():
        return f"频率={int(value)}Hz"
    return f"频率={value}Hz"


def _reading_hop(model: Model, rid: int, note: str | None = None) -> Hop:
    r = model.readings.get(rid)
    if r is None:
        return Hop("reading", f"读数 #{rid}（不存在）", note)
    kind = r.get("kind") or "读数"
    freq = _freq_label(r.get("frequency_hz"))
    t = model.tests.get(r.get("test_id"))
    if t is None:
        where = f"试验 #{r.get('test_id')}（不存在）"
    else:
        g = model.groups.get(t.get("group_id"), {})
        where = (
            f"试验 #{t.get('id')}「{t.get('name', '?')}」"
            f"（组 #{g.get('id', '?')}「{g.get('name', '?')}」）"
        )
    return Hop("reading", f"读数 #{rid}（{kind}｜{freq}｜{where}）", note)


def _is_measurement(reading: dict) -> bool:
    return str(reading.get("kind") or "").strip().lower() == MEASUREMENT_KIND


def _is_control(reading: dict) -> bool:
    return str(reading.get("kind") or "").strip().lower() == CONTROL_KIND


def check_chain(model: Model, start_id: int) -> QualityIssue | None:
    """核查单个 measurement 读数的对照链，返回首个问题（无问题返回 None）。

    沿 control_reading_id 逐跳前进，直到引用为 NULL 的链末端：
    中途的 control 读数若仍带引用则继续追踪；仅末端必须为 control。
    """
    start = model.readings[start_id]
    chain = [_reading_hop(model, start_id)]

    def issue(reason: str) -> QualityIssue:
        return QualityIssue(start["test_id"], start_id, reason, chain)

    if start.get("frequency_hz") is None:
        return issue(REASON_MISSING_FREQUENCY)

    next_id = start.get("control_reading_id")
    if next_id is None:
        return issue(REASON_NO_CONTROL)

    prev_id = start_id
    visited = {start_id}
    while True:
        target = model.readings.get(next_id)
        if target is None:
            chain.append(
                Hop("reading", f"读数 #{next_id}（不存在）", "悬空目标")
            )
            return issue(REASON_DANGLING)

        cur_id = next_id
        chain.append(_reading_hop(model, cur_id))
        prev = model.readings[prev_id]

        if target.get("frequency_hz") is None:
            chain[-1].note = "该读数缺 frequency_hz"
            return issue(REASON_MISSING_FREQUENCY)
        if target["frequency_hz"] != prev["frequency_hz"]:
            chain[-1].note = (
                f"频点与上一跳不一致：{_freq_label(prev['frequency_hz'])} → "
                f"{_freq_label(target['frequency_hz'])}"
            )
            return issue(REASON_FREQUENCY_CONFLICT)
        # 成环检查先于一切收尾判断：回到链上任意已出现读数即成环
        if cur_id in visited:
            chain[-1].note = "回到链上已出现的读数，成环"
            return issue(REASON_CYCLE)
        visited.add(cur_id)

        next_id = target.get("control_reading_id")
        if next_id is None:
            # 已到链末端：末端必须是 control 读数
            if not _is_control(target):
                chain[-1].note = "链末端读数不是 control"
                return issue(REASON_BAD_TERMINAL)
            return None
        prev_id = cur_id

def check_quality(model: Model, res: Resolution | None = None) -> QualityReport:
    """对交付范围内（res.reading_ids）的全部 measurement 读数做对照链核查。

    未传入 res 时按全库解析；起始读数按主键排序，保证报告顺序稳定。
    """
    if res is None:
        res = resolve(model, set(), set())
    report = QualityReport()
    starts = sorted(
        rid
        for rid in res.reading_ids
        if _is_measurement(model.readings[rid])
    )
    report.checked = len(starts)
    for rid in starts:
        found = check_chain(model, rid)
        if found is not None:
            report.issues.append(found)
    return report


def check_quality_for_selection(
    model: Model,
    selected_test_ids: set[int] | list[int],
    excluded_reading_ids: set[int] | list[int],
) -> tuple[Resolution, QualityReport]:
    """Web 层入口：先做交付解析，再在同一交付范围内做质量核查。"""
    res = resolve(model, selected_test_ids, excluded_reading_ids)
    return res, check_quality(model, res)
