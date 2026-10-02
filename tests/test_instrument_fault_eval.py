"""仪器失准故障时段遮蔽的纯函数测试（app.evaluation.evaluate）。

运行：PYTHONPATH=. python -m pytest tests/test_instrument_fault_eval.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.evaluation import evaluate

BASE = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def at(minute: float) -> datetime:
    return BASE + timedelta(minutes=minute)


def readings_every_minute(temp: float = 23.0, n: int = 11):
    """0..n-1 分钟各一条读数（末条恰在窗口终点作覆盖终止采样）。"""
    return [(at(i), temp) for i in range(n)]


def run(faults=None, temp=23.0, max_exceed=300.0, max_gap=1200.0, minutes=10,
        readings=None):
    if readings is None:
        readings = readings_every_minute(temp=temp, n=minutes + 1)
    return evaluate(
        window_start=BASE, window_end=at(minutes),
        temp_min=18.0, temp_max=22.0,
        max_exceed_seconds=max_exceed,
        max_sample_interval=max_gap,
        readings=readings,
        faults=faults,
    )


# ---------------------------------------------------------------- 基本语义

def test_no_faults_unchanged():
    ev = run()
    assert ev["verdict"] == "fail"
    # 全部超限（0..10 分钟），无未知
    assert ev["has_unknown"] is False
    assert ev["max_continuous_exceeded_seconds"] == 600.0


def test_fault_masks_readings_to_unknown_and_cuts_continuous_exceeded():
    # 遮蔽 [5,10)：超限被切成 [0,5)=300s（不严格大于限值 300），
    # 遮蔽段为未知 -> 结论待定（fail 优先级最高，但超限已不超时）。
    ev = run(faults=[(at(5), at(10))])
    assert ev["verdict"] == "pending"
    assert ev["has_unknown"] is True
    assert ev["max_continuous_exceeded_seconds"] == 300.0
    unknown = ev["unknown_segments"]
    assert len(unknown) == 1
    assert unknown[0]["start"] == "2026-10-01T00:05:00Z"
    assert unknown[0]["end"] == "2026-10-01T00:10:00Z"
    assert unknown[0]["duration_seconds"] == 300.0


def test_fault_in_middle_splits_exceeded_into_two_runs():
    # 遮蔽 [4,6)：左侧超限 240s、右侧 240s，均不超时 -> pending
    ev = run(faults=[(at(4), at(6))])
    assert ev["verdict"] == "pending"
    assert ev["max_continuous_exceeded_seconds"] == 240.0
    exc = ev["exceeded_segments"]
    assert [(s["start"], s["end"]) for s in exc] == [
        ("2026-10-01T00:00:00Z", "2026-10-01T00:04:00Z"),
        ("2026-10-01T00:06:00Z", "2026-10-01T00:10:00Z"),
    ]


def test_fault_masking_normal_readings_also_becomes_unknown():
    # 遮蔽部分无论有无读数、无论原状态均为未知：正常读数被遮蔽也是未知
    ev = run(faults=[(at(2), at(4))], temp=20.0)
    assert ev["verdict"] == "pending"
    assert ev["normal_segments"][0]["start"] == "2026-10-01T00:00:00Z"
    assert ev["normal_segments"][0]["end"] == "2026-10-01T00:02:00Z"
    assert ev["normal_segments"][1]["start"] == "2026-10-01T00:04:00Z"
    assert [(u["start"], u["end"]) for u in ev["unknown_segments"]] == [
        ("2026-10-01T00:02:00Z", "2026-10-01T00:04:00Z"),
    ]


def test_fault_boundary_half_open_end_does_not_mask_beyond():
    # 半开：[2,4) 遮蔽，t=4 的读数从 4 开始的覆盖不再被遮蔽
    ev = run(faults=[(at(2), at(4))], temp=23.0)
    # [0,2) exceeded 120s，[2,4) unknown 120s，[4,10) exceeded 360s
    assert ev["max_continuous_exceeded_seconds"] == 360.0
    assert ev["verdict"] == "fail"  # 360 > 300，未知不影响 fail 优先


def test_fault_covering_whole_window_all_unknown():
    # 故障恰覆盖半开整窗（允许贴窗口边界）
    ev = run(faults=[(at(0), at(10))], temp=23.0, max_exceed=1.0)
    assert ev["verdict"] == "pending"
    assert ev["exceeded_segments"] == []
    assert len(ev["unknown_segments"]) == 1
    assert ev["unknown_segments"][0]["duration_seconds"] == 600.0


def test_overlapping_faults_union():
    # 两段重叠故障取并集：[2,6) 与 [4,8) -> [2,8)
    ev = run(faults=[(at(2), at(6)), (at(4), at(8))])
    assert [(u["start"], u["end"]) for u in ev["unknown_segments"]] == [
        ("2026-10-01T00:02:00Z", "2026-10-01T00:08:00Z"),
    ]


def test_adjacent_faults_union():
    # 首尾相接 [2,4)+[4,8) 并为一段
    ev = run(faults=[(at(2), at(4)), (at(4), at(8))])
    assert [(u["start"], u["end"]) for u in ev["unknown_segments"]] == [
        ("2026-10-01T00:02:00Z", "2026-10-01T00:08:00Z"),
    ]


def test_fault_masked_gap_unknown_merges_with_adjacent_fault_unknown():
    # 采样间隔超限的未知与故障遮蔽未知相邻 -> 合并为一个未知段
    # 读数 t=0,2,8,10；间隔 2->8 为 6 分钟 > 120s（gap 未知 [2,8)），
    # 故障 [8,9) 遮蔽：[2,9) 连续未知合并
    readings = [(at(0), 20.0), (at(2), 20.0), (at(8), 20.0), (at(10), 20.0)]
    ev = run(faults=[(at(8), at(9))], max_gap=120.0, readings=readings)
    assert [(u["start"], u["end"]) for u in ev["unknown_segments"]] == [
        ("2026-10-01T00:02:00Z", "2026-10-01T00:09:00Z"),
    ]


def test_fault_accepts_dict_intervals_and_tz_offset():
    # dict 形式 + 非 UTC 时区（+08:00 表示 00:05Z 起）
    tz8 = timezone(timedelta(hours=8))
    start = datetime(2026, 10, 1, 8, 5, tzinfo=tz8)
    end = datetime(2026, 10, 1, 8, 10, tzinfo=tz8)
    ev = run(faults=[{"start": start, "end": end}])
    assert ev["unknown_segments"][0]["start"] == "2026-10-01T00:05:00Z"


def test_reading_count_includes_masked_readings():
    # 遮蔽不删除读数：reading_count 仍为全部活动读数数
    ev = run(faults=[(at(2), at(8))])
    assert ev["reading_count"] == 11
