"""引用质量核查测试：跨组对照链、频点冲突、缺频点、悬空、成环、末端非对照。

核查规则见 app/quality.py：对交付范围内的 measurement 读数沿
control_reading_id 逐跳检查 frequency_hz 相同，且链末端必须为 control。
"""
import sqlite3

import pytest

from app.core import load_model, resolve
from app.quality import (
    REASON_BAD_TERMINAL,
    REASON_CYCLE,
    REASON_DANGLING,
    REASON_FREQUENCY_CONFLICT,
    REASON_MISSING_FREQUENCY,
    REASON_NO_CONTROL,
    check_chain,
    check_quality,
    check_quality_for_selection,
)


def _reload(sample_db):
    conn = sqlite3.connect(sample_db)
    try:
        return load_model(conn)
    finally:
        conn.close()


def _issue_map(model, test_ids, excluded=set()):
    res, report = check_quality_for_selection(model, set(test_ids), set(excluded))
    return res, report, {i.reading_id: i for i in report.issues}


def test_clean_cross_group_chain_passes(model):
    """跨组同频点链 R101(100Hz) → R901(control,100Hz) 核查通过。"""
    res = resolve(model, {1}, {102, 103})
    report = check_quality(model, res)
    assert report.ok
    assert report.checked == 1  # 仅 R101 一条 measurement


def test_sample_db_flags_frequency_conflict(model):
    """样例库 R102(125Hz) → R901(100Hz) 为频点冲突，链完整且按试验汇总。"""
    res, report, issues = _issue_map(model, {1})
    assert not report.ok
    issue = issues[102]
    assert issue.reason == REASON_FREQUENCY_CONFLICT
    assert issue.test_id == 1
    chain_ids = [h.label for h in issue.chain]
    assert any("102" in x for x in chain_ids)
    assert any("901" in x for x in chain_ids)
    assert issue.chain[-1].note and "不一致" in issue.chain[-1].note
    # 按受影响试验汇总
    assert set(report.by_test) == {1}


def test_chained_cross_group_conflict(model):
    """跨组链式引用 R201(100Hz) → R902(125Hz) → R901(100Hz)：首跳即频点冲突。"""
    res, report, issues = _issue_map(model, {3})
    issue = issues[201]
    assert issue.reason == REASON_FREQUENCY_CONFLICT
    # 完整链包含 R902，且问题标注在 R902 这一跳
    assert any("902" in h.label for h in issue.chain)
    assert "902" in issue.chain[-1].label


def test_missing_frequency_on_start(model, sample_db):
    """起始 measurement 的 frequency_hz 为空 → 缺频点。"""
    conn = sqlite3.connect(sample_db)
    conn.execute("UPDATE readings SET frequency_hz = NULL WHERE id = 101")
    conn.commit()
    conn.close()
    m = _reload(sample_db)
    res, report, issues = _issue_map(m, {1}, {102, 103})
    assert issues[101].reason == REASON_MISSING_FREQUENCY


def test_missing_frequency_mid_chain(model, sample_db):
    """链中间读数缺频点：R101(100) → R901(freq NULL)，停止追链并报缺频点。"""
    conn = sqlite3.connect(sample_db)
    conn.execute("UPDATE readings SET frequency_hz = NULL WHERE id = 901")
    conn.commit()
    conn.close()
    m = _reload(sample_db)
    res, report, issues = _issue_map(m, {1}, {102, 103})
    issue = issues[101]
    assert issue.reason == REASON_MISSING_FREQUENCY
    assert "901" in issue.chain[-1].label


def test_dangling_control_chain(model, sample_db):
    """对照引用悬空：measurement → 不存在的读数，完整链含悬空目标。"""
    conn = sqlite3.connect(sample_db)
    conn.execute(
        "INSERT INTO readings VALUES "
        "(199, 2, NULL, 999, 'measurement', 200.0, 48.0, NULL)"
    )
    conn.commit()
    conn.close()
    m = _reload(sample_db)
    res, report, issues = _issue_map(m, {2})
    issue = issues[199]
    assert issue.reason == REASON_DANGLING
    assert "199" in issue.chain[0].label
    assert "999" in issue.chain[-1].label


def test_cycle_chain_reported(model, sample_db):
    """R201 → R902 → R901 ↔ R902 成环：报告成环并展示闭环完整链。"""
    conn = sqlite3.connect(sample_db)
    # 让频点先一致，确保问题确实是成环而非频点冲突
    conn.execute("UPDATE readings SET frequency_hz = 100.0 WHERE id IN (901, 902)")
    conn.execute("UPDATE readings SET control_reading_id = 902 WHERE id = 901")
    conn.commit()
    conn.close()
    m = _reload(sample_db)
    res, report, issues = _issue_map(m, {3}, {202})
    issue = issues[201]
    assert issue.reason == REASON_CYCLE
    labels = [h.label for h in issue.chain]
    assert any("201" in x for x in labels)
    assert any("902" in x for x in labels)
    assert any("901" in x for x in labels)
    # 闭环：R902 在链上出现两次
    assert sum("902" in x for x in labels) == 2
    assert issue.chain[-1].note and "成环" in issue.chain[-1].note


def test_self_loop_reported(model, sample_db):
    """自引用成环：R101.control → R101。"""
    conn = sqlite3.connect(sample_db)
    conn.execute("UPDATE readings SET control_reading_id = 101 WHERE id = 101")
    conn.commit()
    conn.close()
    m = _reload(sample_db)
    issue = check_chain(m, 101)
    assert issue is not None
    assert issue.reason == REASON_CYCLE


def test_chain_ending_in_measurement(model, sample_db):
    """链末端是 measurement（无 control 收尾）→ 链末端非对照。"""
    conn = sqlite3.connect(sample_db)
    # R101(100) → R111(measurement,100, 无对照)
    conn.execute("UPDATE readings SET control_reading_id = 111 WHERE id = 101")
    conn.commit()
    conn.close()
    m = _reload(sample_db)
    res, report, issues = _issue_map(m, {1, 2}, {102, 103, 112})
    issue = issues[101]
    assert issue.reason == REASON_BAD_TERMINAL
    assert "111" in issue.chain[-1].label


def test_measurement_without_control(model, sample_db):
    """measurement 没有 control_reading_id → 无对照问题。"""
    m = model
    issue = check_chain(m, 103)  # R103 在样例库中无对照引用
    assert issue is not None
    assert issue.reason == REASON_NO_CONTROL


def test_excluded_measurements_not_checked(model):
    """被剔除的 measurement 不纳入核查范围。"""
    res = resolve(model, {1}, {102, 103})
    report = check_quality(model, res)
    assert report.checked == 1
    assert {i.reading_id for i in report.issues} == set()


def test_issues_grouped_by_affected_test(model, sample_db):
    """多个试验出问题时按受影响试验分组，组内为该试验起始的问题链。"""
    conn = sqlite3.connect(sample_db)
    # T2 的 R111 改为悬空对照，制造第二个受影响试验
    conn.execute(
        "INSERT INTO readings VALUES "
        "(199, 2, NULL, 999, 'measurement', 200.0, 48.0, NULL)"
    )
    conn.commit()
    conn.close()
    m = _reload(sample_db)
    res, report = check_quality_for_selection(m, {1, 2}, set())
    grouped = report.by_test
    assert 1 in grouped and 2 in grouped
    assert all(i.test_id == 1 for i in grouped[1])
    assert any(i.reading_id == 199 for i in grouped[2])


def test_only_measurement_readings_checked(model):
    """control 读数自身不作为核查起点（即使它也带 control_reading_id）。"""
    res = resolve(model, {9}, set())
    report = check_quality(model, res)
    # T9 全是 control，没有 measurement 起点
    assert report.checked == 0
    assert report.ok


def test_all_clean_selection_passes(model, sample_db):
    """构造全干净选择：同频点 measurement→control 且无缺漏，核查通过。"""
    conn = sqlite3.connect(sample_db)
    # 新增一条与 R901 同频点、正常收尾的 measurement
    conn.execute(
        "INSERT INTO readings VALUES "
        "(301, 2, NULL, 901, 'measurement', 100.0, 50.0, NULL)"
    )
    conn.commit()
    conn.close()
    m = _reload(sample_db)
    res, report = check_quality_for_selection(m, {2}, {111, 112})
    assert report.ok
    assert report.checked == 1


def test_empty_selection_has_nothing_to_check(model):
    """不勾选任何试验：无 measurement 起点，核查通过。"""
    res, report = check_quality_for_selection(model, set(), set())
    assert report.checked == 0
    assert report.ok


def test_integer_and_float_equal_frequency_not_conflict(model, sample_db):
    """100 与 100.0 在 SQLite 中数值相等，不应误报频点冲突。"""
    conn = sqlite3.connect(sample_db)
    conn.execute(
        "INSERT INTO readings(id, test_id, control_reading_id, kind, frequency_hz) "
        "VALUES (302, 2, 901, 'measurement', 100)"
    )
    conn.commit()
    conn.close()
    m = _reload(sample_db)
    issue = check_chain(m, 302)
    assert issue is None
