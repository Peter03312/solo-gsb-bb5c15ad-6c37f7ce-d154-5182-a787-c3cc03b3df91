"""本轮（仪器失准故障）端到端调用示例脚本：仅标准库。

用法：BASE_URL=http://127.0.0.1:8000 python3 examples/instrument_fault_flow.py

流程：建窗（连续超限会不合格的布置）→ 签发 v1(fail) → 登记故障立即重算
（故障边界切断连续超限，变 pending）→ 同标识重放幂等/异内容 409 →
签发 v2（快照固化故障）→ 跨证修订影响报告 → 撤回故障（同因幂等/异因 409）
→ 当前审定恢复 fail；另演示非法时段 422、未知标识 404、空原因 422。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def iso(minute: int) -> str:
    return (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")


def main() -> None:
    # 1) 建窗：60 分钟，允许 18..22，连续超限限值 2000s，采样间隔 1200s
    _, win = call("POST", "/api/v1/windows", {
        "name": "展柜A-故障演示",
        "start_at": iso(0), "end_at": iso(60),
        "allowed_temp_min": 18.0, "allowed_temp_max": 22.0,
        "max_continuous_exceed_seconds": 2000.0,
        "max_sample_interval_seconds": 1200.0})
    wid = win["id"]
    print(f"[建窗] id={wid}")

    # 2) 每分钟一条 23°C（超限）读数，末读恰在终点 -> 连续超限 3600s，fail
    for m in range(61):
        call("POST", f"/api/v1/windows/{wid}/readings", {
            "reading_id": f"r{m:02d}", "revision": 1,
            "measured_at": iso(m), "temperature": 23.0})
    _, ev0 = call("GET", f"/api/v1/windows/{wid}/evaluation")
    print(f"[无故障] verdict={ev0['verdict']} "
          f"最长连续超限={ev0['max_continuous_exceeded_seconds']}s")

    # 3) 签发 v1（故障功能之前的状态；旧证快照 instrument_faults 为空）
    _, c1 = call("POST", f"/api/v1/windows/{wid}/certificates/issue",
                 {"note": "故障前"})
    print(f"[签发 v1] id={c1['id']} verdict={c1['verdict']} "
          f"快照故障数={len(c1['snapshot']['instrument_faults'])}")

    # 4) 拒收演示：起点晚于终点 422；空原因 422；撤回未知标识 404
    code, err = call("POST", f"/api/v1/windows/{wid}/instrument-faults", {
        "fault_id": "BAD", "start_at": iso(40), "end_at": iso(30),
        "reason": "倒置时段"})
    print(f"[非法时段] HTTP {code}：{err['detail']}")
    code, _ = call("POST", f"/api/v1/windows/{wid}/instrument-faults", {
        "fault_id": "BAD", "start_at": iso(10), "end_at": iso(20),
        "reason": "   "})
    print(f"[空原因] HTTP {code}")
    code, _ = call("POST",
                   f"/api/v1/windows/{wid}/instrument-faults/NOPE/withdrawal",
                   {"reason": "撤回不存在的故障"})
    print(f"[撤回未知标识] HTTP {code}")

    # 5) 登记故障 F1：半开 [30,40)，用 +08:00 时区演示归一到 UTC
    code, reg = call("POST", f"/api/v1/windows/{wid}/instrument-faults", {
        "fault_id": "F1",
        "start_at": "2026-10-01T08:30:00+08:00",
        "end_at": "2026-10-01T08:40:00+08:00",
        "reason": "馆员确认温度计 00:30-00:40 失准"})
    ev = reg["current_evaluation"]
    print(f"[登记故障] HTTP {code} effect={reg['effect']} "
          f"UTC={reg['fault']['start_at']}~{reg['fault']['end_at']} "
          f"verdict={ev['verdict']} 最长连续超限="
          f"{ev['max_continuous_exceeded_seconds']}s（被故障边界切成 1800/1200）"
          f" 未知段={[(u['start'][11:16], u['end'][11:16]) for u in ev['unknown_segments']]}")

    # 6) 同标识同内容重放 -> idempotent；异内容 -> 409
    _, replay = call("POST", f"/api/v1/windows/{wid}/instrument-faults", {
        "fault_id": "F1", "start_at": iso(30), "end_at": iso(40),
        "reason": "馆员确认温度计 00:30-00:40 失准"})
    code, _ = call("POST", f"/api/v1/windows/{wid}/instrument-faults", {
        "fault_id": "F1", "start_at": iso(30), "end_at": iso(41),
        "reason": "馆员确认温度计 00:30-00:40 失准"})
    print(f"[同内容重放] effect={replay['effect']}；[异内容] HTTP {code}")

    # 7) 签发 v2（快照固化有效故障与结果）
    _, c2 = call("POST", f"/api/v1/windows/{wid}/certificates/issue",
                 {"note": "故障遮蔽中"})
    print(f"[签发 v2] id={c2['id']} verdict={c2['verdict']} "
          f"快照故障={[f['fault_id'] for f in c2['snapshot']['instrument_faults']]}")

    # 8) 跨证修订影响报告：从两证各自快照还原故障遮蔽的时间线变化
    _, report = call("GET",
                     f"/api/v1/certificates/revision-report"
                     f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}")
    ch = [x for x in report["timeline"]["changes"]
          if x["start"] == iso(30) and x["end"] == iso(40)][0]
    print(f"[修订影响报告] {report['summary']['verdict_before']} -> "
          f"{report['summary']['verdict_after']}；"
          f"故障差异 registered={report['instrument_faults_diff']['registered_count']}；"
          f"[30,40) {ch['before']['status']} -> {ch['after']['status']} "
          f"证据={ch['after']['evidence']}")

    # 9) 撤回 F1：同因重放幂等、异因 409；撤回后审定恢复
    code, wd = call("POST",
                    f"/api/v1/windows/{wid}/instrument-faults/F1/withdrawal",
                    {"reason": "校准恢复"})
    ev = wd["current_evaluation"]
    print(f"[撤回故障] HTTP {code} effect={wd['effect']} "
          f"verdict={ev['verdict']} 最长连续超限="
          f"{ev['max_continuous_exceeded_seconds']}s has_unknown={ev['has_unknown']}")
    _, wd2 = call("POST",
                  f"/api/v1/windows/{wid}/instrument-faults/F1/withdrawal",
                  {"reason": "校准恢复"})
    code, _ = call("POST",
                   f"/api/v1/windows/{wid}/instrument-faults/F1/withdrawal",
                   {"reason": "另一个原因"})
    print(f"[撤回重放] effect={wd2['effect']}（撤回时间不变）；[异因] HTTP {code}")

    # 10) 旧证快照不受撤回影响
    _, c2b = call("GET", f"/api/v1/certificates/{c2['id']}")
    print(f"[旧证快照不变] v2 verdict={c2b['verdict']} "
          f"快照故障={[f['fault_id'] for f in c2b['snapshot']['instrument_faults']]}")

    print("OK: 仪器失准故障端到端流程完成")


if __name__ == "__main__":
    main()
