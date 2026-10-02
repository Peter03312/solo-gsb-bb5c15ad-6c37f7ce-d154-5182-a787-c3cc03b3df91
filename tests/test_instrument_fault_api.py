"""仪器失准故障时段 HTTP 集成测试。

覆盖：
- 登记（标识/非空原因/带时区半开时段、完全落在窗口内、start<end）与立即重算；
- 同标识同内容重放幂等、异内容 409；已撤回标识再登记 409；
- 撤回同因幂等、异因 409、未知标识 404；非法时段/空原因 422 且不改状态；
- 故障遮蔽立即改变审定（故障边界切断连续超限），撤回后恢复；
- 新签证书固化有效故障时段及结果，旧证快照不变；
- 跨证修订影响报告从各自快照还原故障造成的时间线变化（旧快照缺字段视为空）；
- 窗口详情/列表携带有效故障；既有读数/规则/证书/移交接口字段含义不变。

运行：PYTHONPATH=. python -m pytest tests/test_instrument_fault_api.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    from app.main import app

    with TestClient(app) as c:
        yield c


BASE = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def iso(minute: float, hour: int = 0) -> str:
    return (BASE + timedelta(hours=hour, minutes=minute)).isoformat().replace("+00:00", "Z")


def make_window(client, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=1200.0):
    r = client.post("/api/v1/windows", json={
        "start_at": iso(0), "end_at": iso(minutes),
        "allowed_temp_min": tmin, "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap})
    assert r.status_code == 201, r.text
    return r.json()


def post_reading(client, wid, rid, minute, temp, rev=1):
    r = client.post(f"/api/v1/windows/{wid}/readings", json={
        "reading_id": rid, "revision": rev,
        "measured_at": iso(minute), "temperature": temp})
    assert r.status_code == 201, r.text
    return r.json()


def add_fault(client, wid, fid, s, e, reason):
    return client.post(f"/api/v1/windows/{wid}/instrument-faults", json={
        "fault_id": fid, "start_at": iso(s), "end_at": iso(e), "reason": reason})


def eval_segments(ev):
    return {k: [(x["start"], x["end"]) for x in ev[k]]
            for k in ("normal_segments", "exceeded_segments", "unknown_segments")}


def make_fail_window(client):
    """窗口 60min、每分钟一条 23°C 读数（18..22 允许，超限 3600s -> fail）。"""
    w = make_window(client)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 23.0)
    ev = client.get(f"/api/v1/windows/{w['id']}/evaluation").json()
    assert ev["verdict"] == "fail"
    return w


# ---------------------------------------------------------------- 登记

def test_register_fault_masks_and_recuts_immediately(client):
    w = make_fail_window(client)
    r = add_fault(client, w["id"], "F1", 30, 40, "馆员确认温度计 00:30-00:40 失准")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "registered"
    assert body["fault"]["fault_id"] == "F1"
    assert body["fault"]["status"] == "active"
    assert body["fault"]["start_at"] == iso(30)
    assert body["fault"]["end_at"] == iso(40)
    assert body["fault"]["withdrawn_reason"] is None
    # 遮蔽 600s 把超限切成 1800 / 1200，均不超过 300s 限值？限值 300s 仍超时
    ev = body["current_evaluation"]
    assert ev["max_continuous_exceeded_seconds"] == 1800.0
    assert ev["verdict"] == "fail"  # 1800 > 300 仍不合格，但连续时长被切断
    unknown = [(u["start"], u["end"]) for u in ev["unknown_segments"]]
    assert (iso(30), iso(40)) in unknown


def test_register_fault_can_change_fail_to_pending(client):
    # 限值放宽到 2000s：遮蔽后最长连续超限 1800s 不超时，存在未知 -> pending
    w = make_window(client, max_exceed=2000.0)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 23.0)
    r = add_fault(client, w["id"], "F1", 30, 40, "失准")
    assert r.status_code == 201
    assert r.json()["current_evaluation"]["verdict"] == "pending"


def test_register_fault_listed_in_window_detail_and_list(client):
    w = make_window(client)
    add_fault(client, w["id"], "F1", 10, 20, "失准")
    detail = client.get(f"/api/v1/windows/{w['id']}").json()
    faults = detail["active_instrument_faults"]
    assert [f["fault_id"] for f in faults] == ["F1"]
    listed = client.get("/api/v1/windows").json()
    row = next(x for x in listed if x["id"] == w["id"])
    assert [f["fault_id"] for f in row["active_instrument_faults"]] == ["F1"]


def test_fault_get_endpoints(client):
    w = make_window(client)
    add_fault(client, w["id"], "F1", 10, 20, "失准")
    r = client.get(f"/api/v1/windows/{w['id']}/instrument-faults")
    assert r.status_code == 200
    assert [f["fault_id"] for f in r.json()] == ["F1"]
    active = client.get(
        f"/api/v1/windows/{w['id']}/instrument-faults", params={"active_only": "true"})
    assert [f["fault_id"] for f in active.json()] == ["F1"]


# ---------------------------------------------------------------- 幂等/冲突

def test_replay_same_content_idempotent(client):
    w = make_fail_window(client)
    r1 = add_fault(client, w["id"], "F1", 30, 40, "失准")
    r2 = add_fault(client, w["id"], "F1", 30, 40, "失准")
    assert r1.json()["fault"]["registered_at"] == r2.json()["fault"]["registered_at"]
    assert r2.json()["effect"] == "idempotent"
    rows = client.get(f"/api/v1/windows/{w['id']}/instrument-faults").json()
    assert len(rows) == 1


def test_same_id_different_interval_conflict_and_state_unchanged(client):
    w = make_window(client)
    assert add_fault(client, w["id"], "F1", 10, 20, "失准").status_code == 201
    r = add_fault(client, w["id"], "F1", 10, 21, "失准")
    assert r.status_code == 409
    faults = client.get(f"/api/v1/windows/{w['id']}/instrument-faults").json()
    assert faults[0]["end_at"] == iso(20)  # 状态不变


def test_same_id_different_reason_conflict(client):
    w = make_window(client)
    add_fault(client, w["id"], "F1", 10, 20, "原因甲")
    r = add_fault(client, w["id"], "F1", 10, 20, "原因乙")
    assert r.status_code == 409
    faults = client.get(f"/api/v1/windows/{w['id']}/instrument-faults").json()
    assert faults[0]["reason"] == "原因甲"


def test_withdrawn_id_cannot_register_again(client):
    w = make_window(client)
    add_fault(client, w["id"], "F1", 10, 20, "失准")
    wr = client.post(
        f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
        json={"reason": "校准恢复，撤回"})
    assert wr.status_code == 201
    r = add_fault(client, w["id"], "F1", 10, 20, "失准")
    assert r.status_code == 409
    # 同内容重放也不幂等（已撤回）；记录仍是 withdrawn
    faults = client.get(f"/api/v1/windows/{w['id']}/instrument-faults").json()
    assert faults[0]["status"] == "withdrawn"


# ---------------------------------------------------------------- 撤回

def test_withdraw_same_reason_idempotent_different_conflict(client):
    w = make_window(client)
    add_fault(client, w["id"], "F1", 10, 20, "失准")
    r1 = client.post(
        f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
        json={"reason": "校准恢复"})
    assert r1.status_code == 201
    assert r1.json()["effect"] == "withdrawn"
    withdrawn_at = r1.json()["fault"]["withdrawn_at"]

    r2 = client.post(
        f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
        json={"reason": "校准恢复"})
    assert r2.status_code == 201
    assert r2.json()["effect"] == "idempotent"
    assert r2.json()["fault"]["withdrawn_at"] == withdrawn_at

    r3 = client.post(
        f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
        json={"reason": "另一个原因"})
    assert r3.status_code == 409


def test_withdraw_unknown_id_404(client):
    w = make_window(client)
    r = client.post(
        f"/api/v1/windows/{w['id']}/instrument-faults/NOPE/withdrawal",
        json={"reason": "撤回"})
    assert r.status_code == 404


def test_withdraw_restores_evaluation(client):
    w = make_window(client, max_exceed=2000.0)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 23.0)
    assert add_fault(client, w["id"], "F1", 30, 40, "失准").json()[
        "current_evaluation"]["verdict"] == "pending"
    wr = client.post(
        f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
        json={"reason": "校准恢复"})
    ev = wr.json()["current_evaluation"]
    # 撤回后无未知；连续超限 3600 > 2000 -> fail（故障边界不再切断）
    assert ev["verdict"] == "fail"
    assert ev["has_unknown"] is False
    assert ev["max_continuous_exceeded_seconds"] == 3600.0
    # 撤回后不再出现在有效列表
    detail = client.get(f"/api/v1/windows/{w['id']}").json()
    assert detail["active_instrument_faults"] == []
    all_rows = client.get(f"/api/v1/windows/{w['id']}/instrument-faults").json()
    assert all_rows[0]["status"] == "withdrawn"
    assert all_rows[0]["withdrawn_reason"] == "校准恢复"


# ---------------------------------------------------------------- 422 / 404

@pytest.mark.parametrize("s,e,detail_kw", [
    (10, 10, "严格早于"),       # start == end
    (20, 10, "严格早于"),       # start > end
    (-1, 10, "完全落在窗口"),   # 早于窗口起点
    (10, 61, "完全落在窗口"),   # 晚于窗口终点
])
def test_invalid_interval_422_and_no_state_change(client, s, e, detail_kw):
    w = make_window(client)
    r = add_fault(client, w["id"], "F1", s, e, "失准")
    assert r.status_code == 422, r.text
    assert detail_kw in r.json()["detail"]
    rows = client.get(f"/api/v1/windows/{w['id']}/instrument-faults").json()
    assert rows == []


def test_interval_exactly_on_window_boundaries_allowed(client):
    w = make_window(client)
    r = add_fault(client, w["id"], "F1", 0, 60, "覆盖整窗")
    assert r.status_code == 201, r.text


def test_blank_reason_422(client):
    w = make_window(client)
    r = client.post(f"/api/v1/windows/{w['id']}/instrument-faults", json={
        "fault_id": "F1", "start_at": iso(10), "end_at": iso(20), "reason": "   "})
    assert r.status_code == 422
    wr = client.post(
        f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
        json={"reason": "\t"})
    assert wr.status_code == 422


def test_naive_datetime_422(client):
    w = make_window(client)
    r = client.post(f"/api/v1/windows/{w['id']}/instrument-faults", json={
        "fault_id": "F1", "start_at": "2026-10-01T00:10:00",
        "end_at": iso(20), "reason": "失准"})
    assert r.status_code == 422


def test_register_window_not_found_404(client):
    r = add_fault(client, 999, "F1", 0, 10, "失准")
    assert r.status_code == 404


def test_withdraw_window_not_found_404(client):
    r = client.post("/api/v1/windows/999/instrument-faults/F1/withdrawal",
                    json={"reason": "撤回"})
    assert r.status_code == 404


def test_timezone_offset_accepted_and_normalized(client):
    w = make_window(client)
    r = client.post(f"/api/v1/windows/{w['id']}/instrument-faults", json={
        "fault_id": "F1",
        "start_at": "2026-10-01T08:10:00+08:00",
        "end_at": "2026-10-01T08:20:00+08:00",
        "reason": "失准"})
    assert r.status_code == 201, r.text
    assert r.json()["fault"]["start_at"] == iso(10)
    assert r.json()["fault"]["end_at"] == iso(20)


# ---------------------------------------------------------------- 证书快照

def test_certificate_snapshot_freezes_faults_and_old_snapshot_unchanged(client):
    w = make_window(client, max_exceed=2000.0)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 23.0)

    c1 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue",
                     json={"note": "故障前"}).json()
    assert c1["verdict"] == "fail"
    assert c1["snapshot"]["instrument_faults"] == []

    add_fault(client, w["id"], "F1", 30, 40, "失准")
    c2 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue",
                     json={"note": "故障中"}).json()
    assert c2["version"] == 2
    assert c2["verdict"] == "pending"
    frozen = c2["snapshot"]["instrument_faults"]
    assert [f["fault_id"] for f in frozen] == ["F1"]
    assert frozen[0]["start_at"] == iso(30) and frozen[0]["end_at"] == iso(40)

    # 撤回故障不改变已签发的 c2 快照；旧证 c1 也完全不变
    client.post(f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
                json={"reason": "校准恢复"})
    c2b = client.get(f"/api/v1/certificates/{c2['id']}").json()
    assert c2b["verdict"] == "pending"
    assert [f["fault_id"] for f in c2b["snapshot"]["instrument_faults"]] == ["F1"]
    c1b = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert c1b["verdict"] == "fail"
    assert c1b["snapshot"]["instrument_faults"] == []
    # 窗口当前审定已恢复（撤回后重算）
    ev = client.get(f"/api/v1/windows/{w['id']}/evaluation").json()
    assert ev["verdict"] == "fail"


def test_revision_report_reflects_fault_masking_from_snapshots(client):
    w = make_window(client, max_exceed=2000.0)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 20.0)  # 全部正常
    c1 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue",
                     json={"note": "故障前 pass"}).json()
    assert c1["verdict"] == "pass"

    add_fault(client, w["id"], "F1", 30, 40, "失准")
    c2 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue",
                     json={"note": "故障中 pending"}).json()
    assert c2["verdict"] == "pending"

    report = client.get("/api/v1/certificates/revision-report",
                        params={"old_certificate_id": c1["id"],
                                "new_certificate_id": c2["id"]}).json()
    # 故障差异：F1 新登记
    fdiff = report["instrument_faults_diff"]
    assert fdiff["registered_count"] == 1
    assert fdiff["registered"][0]["fault_id"] == "F1"
    assert fdiff["withdrawn_count"] == 0
    # 时间线：[30,40) normal -> unknown，证据为 instrument_fault
    changes = report["timeline"]["changes"]
    hit = [c for c in changes if c["start"] == iso(30) and c["end"] == iso(40)]
    assert len(hit) == 1
    assert hit[0]["before"]["status"] == "normal"
    assert hit[0]["after"]["status"] == "unknown"
    assert hit[0]["after"]["evidence"] == {
        "unknown_reason": "instrument_fault", "fault_id": "F1"}
    # 汇总：结论 pass -> pending
    assert report["summary"]["verdict_before"] == "pass"
    assert report["summary"]["verdict_after"] == "pending"
    assert "仪器失准故障时段差异" in report["summary"]["conclusion_zh"]


def test_revision_report_fault_withdrawn_between_certificates(client):
    w = make_window(client, max_exceed=2000.0)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 20.0)
    add_fault(client, w["id"], "F1", 30, 40, "失准")
    c1 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue",
                     json={"note": "故障中"}).json()
    assert c1["verdict"] == "pending"

    client.post(f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
                json={"reason": "校准恢复"})
    c2 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue",
                     json={"note": "撤回后"}).json()
    assert c2["verdict"] == "pass"

    report = client.get("/api/v1/certificates/revision-report",
                        params={"old_certificate_id": c1["id"],
                                "new_certificate_id": c2["id"]}).json()
    fdiff = report["instrument_faults_diff"]
    assert fdiff["withdrawn_count"] == 1
    assert fdiff["withdrawn"][0]["fault_id"] == "F1"
    hit = [c for c in report["timeline"]["changes"]
           if c["start"] == iso(30) and c["end"] == iso(40)]
    assert hit[0]["before"]["status"] == "unknown"
    assert hit[0]["after"]["status"] == "normal"
    assert hit[0]["before"]["evidence"]["fault_id"] == "F1"


def test_report_against_old_certificate_without_fault_field(client):
    """旧证书快照缺 instrument_faults 字段 -> 还原时视为空（不报错）。"""
    from app.report import build_revision_report

    w = make_window(client)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 20.0)
    c1 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue",
                     json={"note": "旧证"}).json()
    add_fault(client, w["id"], "F1", 30, 40, "失准")
    c2 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue",
                     json={"note": "新证"}).json()
    # 直接移除旧证快照字段，模拟故障功能上线前签发的旧证书
    c1["snapshot"].pop("instrument_faults", None)
    report = build_revision_report(c1, c2)
    assert report["instrument_faults_diff"]["registered_count"] == 1
    hit = [c for c in report["timeline"]["changes"]
           if c["start"] == iso(30) and c["end"] == iso(40)]
    assert hit and hit[0]["after"]["evidence"]["fault_id"] == "F1"


# ---------------------------------------------------------------- 故障切断超限的跨证结论变化

def test_fault_boundary_cuts_exceeded_run_priority_recomputed(client):
    # 无故障：连续超限 3600s，fail；故障遮蔽后两段均 1800s <= 限值 2000s
    # 且有未知 -> pending（按 fail > pending > pass 优先级立即重算）。
    w = make_window(client, max_exceed=2000.0)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 23.0)
    c1 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue").json()
    assert c1["verdict"] == "fail"
    add_fault(client, w["id"], "F1", 30, 40, "失准")
    ev = client.get(f"/api/v1/windows/{w['id']}/evaluation").json()
    assert ev["verdict"] == "pending"
    assert ev["max_continuous_exceeded_seconds"] == 1800.0


# ---------------------------------------------------------------- 移交接口不受影响

def test_handover_requires_pass_fault_makes_window_pending(client):
    w = make_window(client, max_exceed=2000.0)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 20.0)
    c = client.post(f"/api/v1/windows/{w['id']}/certificates/issue").json()
    assert c["verdict"] == "pass"
    # 故障登记后再签发 -> pending，该窗口不再满足移交合格条件
    add_fault(client, w["id"], "F1", 30, 40, "失准")
    c2 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue").json()
    assert c2["verdict"] == "pending"
    r = client.post("/api/v1/handover-orders", json={
        "handover_no": "HJ-FAULT-1", "window_ids": [w["id"]]})
    assert r.status_code == 409


# ---------------------------------------------------------------- 多故障并集

def test_multiple_faults_union_masking_and_report_evidence(client):
    # 两段故障 [30,35) F1、[34,40) F2 重叠 -> 并集 [30,40)；
    # 报告里重叠点 34-35 的遮蔽证据为起点最早的 F1（稳定）。
    w = make_window(client, max_exceed=2000.0)
    for m in range(61):
        post_reading(client, w["id"], f"r{m:02d}", m, 20.0)
    c1 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue").json()
    add_fault(client, w["id"], "F1", 30, 35, "失准甲")
    add_fault(client, w["id"], "F2", 34, 40, "失准乙")
    ev = client.get(f"/api/v1/windows/{w['id']}/evaluation").json()
    assert [(u["start"], u["end"]) for u in ev["unknown_segments"]] == [
        (iso(30), iso(40))]
    c2 = client.post(f"/api/v1/windows/{w['id']}/certificates/issue").json()
    report = client.get("/api/v1/certificates/revision-report",
                        params={"old_certificate_id": c1["id"],
                                "new_certificate_id": c2["id"]}).json()
    assert report["instrument_faults_diff"]["registered_count"] == 2
    # 同一故障标识驱动的连续变化分别合并：[30,35) 证据 F1（含被 F2 重叠部分），
    # [35,40) 证据 F2
    by_start = {ch["start"]: ch for ch in report["timeline"]["changes"]}
    assert by_start[iso(30)]["end"] == iso(35)
    assert by_start[iso(30)]["after"]["evidence"]["fault_id"] == "F1"
    assert by_start[iso(35)]["end"] == iso(40)
    assert by_start[iso(35)]["after"]["evidence"]["fault_id"] == "F2"


def test_idempotent_replay_does_not_change_evaluation_or_timestamp(client):
    w = make_window(client)
    r1 = add_fault(client, w["id"], "F1", 10, 20, "失准")
    ev1 = r1.json()["current_evaluation"]
    r2 = add_fault(client, w["id"], "F1", 10, 20, "失准")
    assert r2.status_code == 201
    assert r2.json()["effect"] == "idempotent"
    assert r2.json()["current_evaluation"] == ev1
    assert r2.json()["fault"] == r1.json()["fault"]


def test_withdraw_idempotent_does_not_change_state(client):
    w = make_window(client)
    add_fault(client, w["id"], "F1", 10, 20, "失准")
    r1 = client.post(f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
                     json={"reason": "恢复"})
    r2 = client.post(f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
                     json={"reason": "恢复"})
    assert r1.json()["fault"] == r2.json()["fault"]
    rows = client.get(f"/api/v1/windows/{w['id']}/instrument-faults").json()
    assert len(rows) == 1 and rows[0]["status"] == "withdrawn"


def test_invalid_fault_does_not_block_later_valid_registration(client):
    w = make_window(client)
    bad = add_fault(client, w["id"], "F1", 40, 10, "失准")
    assert bad.status_code == 422  # 事务回滚，标识可重新使用
    ok = add_fault(client, w["id"], "F1", 10, 40, "失准")
    assert ok.status_code == 201
    assert ok.json()["effect"] == "registered"


def test_withdraw_conflict_does_not_change_reason(client):
    w = make_window(client)
    add_fault(client, w["id"], "F1", 10, 20, "失准")
    client.post(f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
                json={"reason": "原因甲"})
    r = client.post(f"/api/v1/windows/{w['id']}/instrument-faults/F1/withdrawal",
                    json={"reason": "原因乙"})
    assert r.status_code == 409
    rows = client.get(f"/api/v1/windows/{w['id']}/instrument-faults").json()
    assert rows[0]["withdrawn_reason"] == "原因甲"
