"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间戳必须带时区信息，例如 2026-10-01T00:00:00Z 或 +08:00")
    return dt


class WindowCreate(BaseModel):
    """创建审定窗口。

    - allowed_temp_min/max：允许温度区间（闭区间，单位 °C）
    - max_continuous_exceed_seconds：最长连续超限时长（秒，超过它判不合格）
    - max_sample_interval_seconds：最大采样间隔（秒，超过则相邻读数间记为未知）
    """

    start_at: datetime = Field(..., description="窗口起始时间（含），带时区")
    end_at: datetime = Field(..., description="窗口结束时间（读数提交含该时刻；输出区段为半开），带时区")
    allowed_temp_min: float = Field(..., description="允许温度下限（含），°C")
    allowed_temp_max: float = Field(..., description="允许温度上限（含），°C")
    max_continuous_exceed_seconds: float = Field(..., gt=0, description="最长连续超限时长，秒")
    max_sample_interval_seconds: float = Field(..., gt=0, description="最大采样间隔，秒")
    name: Optional[str] = Field(None, max_length=200, description="可选备注名，如展柜编号")

    @field_validator("start_at", "end_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("allowed_temp_max")
    @classmethod
    def _temp_range(cls, v: float, info: Any) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v

    @field_validator("allowed_temp_min")
    @classmethod
    def _temp_min_finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v


class ReadingSubmit(BaseModel):
    """提交（或修订）一条温度读数。

    幂等与修订规则（按 同一窗口 + reading_id）：
    - revision 已存在：内容（时间戳/温度）完全一致 -> 幂等返回；
      内容不一致 -> 409 冲突。
    - revision 高于当前 -> 替换当前读数并重算；
    - revision 低于当前 -> 409 拒绝（旧修订号不得覆盖新修订号）。
    """

    reading_id: str = Field(..., min_length=1, max_length=100, description="读数 ID（窗口内唯一）")
    revision: int = Field(..., ge=1, description="修订号，从 1 开始递增")
    measured_at: datetime = Field(..., description="读数时间戳，带时区")
    temperature: float = Field(..., description="温度，°C")

    @field_validator("measured_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("temperature")
    @classmethod
    def _finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v


class ReadingBatchSubmit(BaseModel):
    """采样仪器补传：一个审定窗口下的一批非空读数。

    批次在单个写事务内整体校验、整体提交：任一项冲突或无效即整批拒收
    （活动读数与修订历史均不变）。每项沿用单条提交的 reading_id / revision /
    measured_at / temperature 语义与时区、窗口边界、修订规则；同批不得
    重复 reading_id；同刻唯一按“批次应用后的最终状态”校验，允许两条读数
    以更高修订号互换采样时刻。对已作废的 reading_id，批内以更高修订号
    提交即恢复该读数（effect=restored）。
    """

    readings: list[ReadingSubmit] = Field(
        ..., min_length=1, description="非空读数批次（至少 1 项）；同批 reading_id 不得重复"
    )


class ReadingVoidSubmit(BaseModel):
    """作废一条误采读数：凭递增修订号 + 非空原因。

    - revision 必须高于该读数当前修订号；作废本身作为该 ID 的一条修订
      留存（原因、操作时间、前一版本），当前审定立即剔除该读数并重算。
    - 相同作废修订号 + 相同原因重放 -> 幂等；相同修订号原因不同、
      或修订号不高于当前 -> 409；读数从未存在 -> 404；
      已作废的读数不能再次以新修订号作废 -> 409。
    """

    revision: int = Field(..., ge=1, description="作废修订号，必须高于该读数当前修订号")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="作废原因（非空，不能只含空白字符）")

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("作废原因不能为空（不能只含空白字符）")
        return v


class IssueRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=500, description="签发备注")


# ------------------------------------------------------------ 仪器失准故障

class InstrumentFaultSubmit(BaseModel):
    """馆员确认采样仪器失准：凭窗口内唯一故障标识 + 非空原因 + 带时区
    起止时间登记故障时段（半开 [start_at, end_at)）。

    - 时段必须完全落在审定窗口内（窗口对故障为半开 [start, end)：
      start_at >= 窗口起点、end_at <= 窗口终点），且 start_at 严格早于
      end_at，时间戳必须带时区，否则 422；原因为空或只含空白 -> 422；
    - 同故障标识 + 同时段同原因重放 -> 幂等（effect=idempotent，不产生
      新记录）；同标识但任一字段不同 -> 409；窗口不存在 -> 404；
    - 已撤回的标识不能再次登记（409）；撤回请使用撤回端点。
    """

    fault_id: str = Field(..., min_length=1, max_length=100,
                          description="故障标识（同一窗口内唯一）")
    start_at: datetime = Field(..., description="故障时段起点（含，半开区间），带时区")
    end_at: datetime = Field(..., description="故障时段终点（不含，半开区间），带时区")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="失准确认原因（非空，不能只含空白字符）")

    @field_validator("start_at", "end_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("故障原因不能为空（不能只含空白字符）")
        return v


class InstrumentFaultWithdrawSubmit(BaseModel):
    """撤回仪器失准故障：凭故障标识（路径）+ 非空原因。

    - 撤回只改变故障有效性（状态置 withdrawn，留存撤回原因与时间），
      撤回后该时段不再参与遮蔽并集，当前审定立即重算；
    - 同一故障以相同原因重复撤回 -> 幂等（返回原撤回记录，撤回时间不变）；
      已撤回故障以不同原因再次撤回 -> 409；
    - 原因为空或只含空白 -> 422；故障标识不存在 -> 404。
    """

    reason: str = Field(..., min_length=1, max_length=500,
                        description="撤回原因（非空，不能只含空白字符）")

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("撤回原因不能为空（不能只含空白字符）")
        return v


class InstrumentFaultOut(BaseModel):
    """仪器失准故障记录（有效或已撤回）。"""

    fault_id: str
    start_at: str = Field(..., description="故障时段起点（含，UTC，半开区间）")
    end_at: str = Field(..., description="故障时段终点（不含，UTC，半开区间）")
    reason: str = Field(..., description="登记原因")
    status: str = Field(..., description="active | withdrawn")
    registered_at: str = Field(..., description="登记时间（UTC）")
    withdrawn_reason: Optional[str] = Field(None, description="撤回原因（仅已撤回故障）")
    withdrawn_at: Optional[str] = Field(None, description="撤回操作时间（仅已撤回故障）")


class InstrumentFaultResponse(BaseModel):
    effect: str = Field(..., description="registered | idempotent")
    fault: InstrumentFaultOut
    current_evaluation: EvaluationOut = Field(
        ..., description="登记/重放后按有效故障时段遮蔽立即重算的审定结果")


class InstrumentFaultWithdrawResponse(BaseModel):
    effect: str = Field(..., description="withdrawn | idempotent")
    fault: InstrumentFaultOut
    current_evaluation: EvaluationOut = Field(
        ..., description="撤回后立即重算的审定结果")


# ------------------------------------------------------------ 证书撤销

class CertificateRevokeSubmit(BaseModel):
    """撤销已签发证书：非空原因（证书标识在路径中给出）。

    - 撤销只改变证书有效性：记录撤销原因与撤销时间，证书的版本、结论与
      不可变快照均不改动，历史证书仍可查询/列表/生成修订影响报告；
    - 同一证书以相同原因重复撤销 -> 幂等，返回原撤销记录且不产生新事件；
    - 已撤销证书以不同原因再次撤销 -> 409；原因为空或只含空白 -> 422；
    - 证书不存在 -> 404。
    """

    reason: str = Field(..., min_length=1, max_length=500,
                        description="撤销原因（非空，不能只含空白字符）")

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("撤销原因不能为空（不能只含空白字符）")
        return v


class RevocationInfoOut(BaseModel):
    """撤销信息：证书有效（未撤销）时为 null。"""

    reason: str = Field(..., description="撤销原因")
    revoked_at: str = Field(..., description="撤销操作时间（UTC）")


# ------------------------------------------------------------ 规则修订

class RuleValuesOut(BaseModel):
    """规则四项值（与创建窗口请求体同义）。"""

    allowed_temp_min: float
    allowed_temp_max: float
    max_continuous_exceed_seconds: float
    max_sample_interval_seconds: float


class RuleRevisionSubmit(BaseModel):
    """馆员修订审定窗口规则：窗口号 + 目标规则修订号 + 四项完整新值 + 非空原因。

    窗口初始规则视为第 1 版：
    - revision 等于“当前版 + 1”才生效；修订成功后窗口规则立即更新并按当前
      活动读数重算，后续签发采用新规则，既有证书快照不变；
    - revision 等于当前版且四项规则值与原因均与已存修订一致 -> 幂等重放
      （不产生新记录）；同号但任一值或原因不一致 -> 409；
    - revision 小于当前版（旧号）或大于“当前版 + 1”（跳号）-> 409；
    - 四项数值必须有限、温度下限不大于上限、两个时长为正（gt=0）。
    """

    revision: int = Field(..., ge=1, description="目标规则修订号（须等于当前版 + 1；重放则等于当前版）")
    allowed_temp_min: float = Field(..., description="新的允许温度下限（含），°C")
    allowed_temp_max: float = Field(..., description="新的允许温度上限（含），°C")
    max_continuous_exceed_seconds: float = Field(..., gt=0, description="新的最长连续超限时长，秒（正）")
    max_sample_interval_seconds: float = Field(..., gt=0, description="新的最大采样间隔，秒（正）")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="修订原因（非空，不能只含空白字符）")

    @field_validator("allowed_temp_min", "allowed_temp_max")
    @classmethod
    def _temp_finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("温度必须为有限数值")
        return v

    @field_validator("max_continuous_exceed_seconds", "max_sample_interval_seconds")
    @classmethod
    def _duration_finite_positive(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("时长必须为有限数值")
        if v <= 0:
            raise ValueError("时长必须为正数")
        return v

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("修订原因不能为空（不能只含空白字符）")
        return v


class RuleRevisionOut(BaseModel):
    """一条规则修订记录（第 1 版为窗口初始规则，before 四项为 null）。"""

    rule_revision: int
    before: Optional[RuleValuesOut] = Field(
        None, description="修订前四项规则值；第 1 版（初始规则）为 null")
    after: RuleValuesOut = Field(..., description="修订后（该版生效）四项规则值")
    reason: str
    revised_at: str = Field(..., description="修订操作时间（UTC）；第 1 版为窗口创建时间")


class RuleRevisionResponse(BaseModel):
    effect: str = Field(..., description="revised | idempotent")
    rule_revision: RuleRevisionOut
    current_evaluation: EvaluationOut = Field(
        ..., description="按新规则与当前活动读数立即重算的审定结果")


class SegmentOut(BaseModel):
    start: str
    end: str
    status: str
    duration_seconds: float


class EvaluationOut(BaseModel):
    verdict: str
    normal_segments: list[SegmentOut]
    exceeded_segments: list[SegmentOut]
    unknown_segments: list[SegmentOut]
    max_continuous_exceeded_seconds: float
    max_exceed_seconds: float
    has_exceeded: bool
    has_unknown: bool
    reading_count: int


class ReadingOut(BaseModel):
    reading_id: str
    revision: int
    measured_at: str
    temperature: float
    status: str
    created_at: str


class WindowOut(BaseModel):
    id: int
    name: Optional[str]
    start_at: str
    end_at: str
    allowed_temp_min: float
    allowed_temp_max: float
    max_continuous_exceed_seconds: float
    max_sample_interval_seconds: float
    rule_revision: int = Field(1, description="当前规则修订号（初始规则为第 1 版）")
    created_at: str
    current_evaluation: Optional[EvaluationOut] = None
    active_readings: list[ReadingOut] = []
    latest_certificate_version: Optional[int] = Field(
        None, description="最新签发版本号（不论是否撤销；无证书为 null）")
    latest_verdict: Optional[str] = Field(
        None, description="最新签发版本的结论（不论是否撤销）")
    current_certificate_id: Optional[int] = Field(
        None, description="当前可用证书主键：仅最新签发版本未撤销时存在，否则为 null")
    current_certificate_version: Optional[int] = Field(
        None, description="当前可用证书版本；撤销最新版不自动退回旧版，故可能为 null")
    current_verdict: Optional[str] = Field(None, description="当前可用证书结论；无则为 null")
    current_certificate: Optional["CertificateOut"] = Field(
        None, description="窗口详情中给出的当前可用证书完整内容（列表端点不返回）；无则为 null")
    active_instrument_faults: list["InstrumentFaultOut"] = Field(
        default_factory=list,
        description="当前有效（未撤回）的仪器失准故障时段，按起点升序；列表端点同样返回")


class ReadingSubmitResponse(BaseModel):
    effect: str = Field(..., description="created | updated | restored | idempotent")
    reading: ReadingOut
    current_evaluation: EvaluationOut


class ReadingBatchItemResult(BaseModel):
    effect: str = Field(..., description="created | updated | restored | idempotent")
    reading: ReadingOut


class ReadingBatchSubmitResponse(BaseModel):
    results: list[ReadingBatchItemResult] = Field(
        ..., description="逐项结果，顺序与请求一致；每项给出 created/updated/restored/idempotent 及最终读数"
    )
    current_evaluation: EvaluationOut = Field(
        ..., description="按批次最终状态一次性重算的审定结果"
    )


# ------------------------------------------------------------ 误采作废

class VoidedPreviousOut(BaseModel):
    """被作废的前一版本读数内容。"""

    revision: int
    measured_at: str
    temperature: float


class VoidRecordOut(BaseModel):
    """作废修订记录：作废作为该 ID 的一条修订留存。"""

    reading_id: str
    revision: int = Field(..., description="作废修订号")
    status: str = Field(..., description="固定为 voided")
    reason: str = Field(..., description="作废原因")
    voided_at: str = Field(..., description="作废操作时间（UTC）")
    previous: VoidedPreviousOut = Field(..., description="被作废的前一版本读数")


class ReadingVoidResponse(BaseModel):
    effect: str = Field(..., description="voided | idempotent")
    void: VoidRecordOut
    current_evaluation: EvaluationOut = Field(
        ..., description="剔除该读数后立即重算的审定结果"
    )


class ReadingRevisionOut(BaseModel):
    """读数修订历史行（含作废记录）。"""

    revision: int
    status: str = Field(..., description="active | superseded | voided")
    measured_at: str
    temperature: float
    created_at: str
    void_reason: Optional[str] = Field(None, description="作废原因（仅 voided 行）")
    voided_at: Optional[str] = Field(None, description="作废操作时间（仅 voided 行）")
    prev_revision: Optional[int] = Field(None, description="被作废的前一版本修订号（仅 voided 行）")


class CertificateOut(BaseModel):
    id: int
    window_id: int
    version: int
    verdict: str
    verdict_label_zh: str
    issued_at: str
    note: Optional[str]
    revoked: bool = Field(False, description="是否已撤销（撤销只改变有效性，版本/结论/快照不变）")
    revocation: Optional[RevocationInfoOut] = Field(
        None, description="撤销信息（原因与撤销时间）；未撤销为 null")
    snapshot: dict[str, Any]


class CertificateRevokeResponse(BaseModel):
    effect: str = Field(..., description="revoked | idempotent")
    certificate: CertificateOut = Field(..., description="撤销后的证书（含撤销信息；快照原样）")
    current_certificate: Optional[CertificateOut] = Field(
        None, description="该窗口当前可用证书：仅最新签发版本未撤销时存在，否则为 null")


# ------------------------------------------------------------ 修订影响报告

class ReportReading(BaseModel):
    reading_id: str
    revision: int
    measured_at: str
    temperature: float


class ReportReadingChange(BaseModel):
    reading_id: str
    before: ReportReading
    after: ReportReading
    changed_fields: list[str] = Field(..., description="revision | measured_at | temperature")


class ReportReadingsDiff(BaseModel):
    added: list[ReportReading]
    removed: list[ReportReading]
    changed: list[ReportReadingChange]
    added_count: int
    removed_count: int
    changed_count: int


# ----------------------------------------------------- 仪器失准故障差异

class ReportFault(BaseModel):
    fault_id: str
    start_at: str
    end_at: str
    reason: str


class ReportFaultChange(BaseModel):
    fault_id: str
    before: ReportFault
    after: ReportFault
    changed_fields: list[str] = Field(..., description="start_at | end_at | reason")


class ReportInstrumentFaultsDiff(BaseModel):
    """两证快照有效仪器失准故障时段差异（旧快照缺字段视为空）。

    registered：新证新登记故障；withdrawn：旧证有、新证已撤回（消失）；
    changed：同标识但时段或原因变化。
    """

    registered: list[ReportFault]
    withdrawn: list[ReportFault]
    changed: list[ReportFaultChange]
    registered_count: int
    withdrawn_count: int
    changed_count: int


class ReportTimelineSide(BaseModel):
    status: str
    # 键随状态而定：已知区段 {"reading_id","revision"}；
    # sample_gap 另含 unknown_reason 与 left/right 读数 ID/修订号；
    # boundary_gap 另含 boundary_side=leading|trailing|empty。
    evidence: dict[str, Any]


class ReportTimelineChange(BaseModel):
    start: str
    end: str
    duration_seconds: float
    before: ReportTimelineSide
    after: ReportTimelineSide


class ReportTimeline(BaseModel):
    max_sample_interval_seconds: float
    changes: list[ReportTimelineChange]
    change_count: int


class ReportRulesChange(BaseModel):
    field: str
    label_zh: str
    before: float
    after: float
    delta: float
    unit_zh: str


class ReportRulesDiff(BaseModel):
    """跨规则版本比对：两证快照各自规则修订号与四项规则的前后值。"""

    rule_revision_before: int
    rule_revision_after: int
    rules_changed: bool
    changed_fields: list[str] = Field(
        ..., description="allowed_temp_min | allowed_temp_max | "
                        "max_continuous_exceed_seconds | max_sample_interval_seconds 的子集")
    before: dict[str, Any] = Field(..., description="旧证快照的四项规则值与规则修订号")
    after: dict[str, Any] = Field(..., description="新证快照的四项规则值与规则修订号")
    changes: list[ReportRulesChange]


class ReportCertificateRef(BaseModel):
    id: int
    window_id: int
    version: int
    verdict: str
    verdict_label_zh: str
    issued_at: str
    note: Optional[str]
    revoked: bool = False
    revocation: Optional[RevocationInfoOut] = None


class ReportSummary(BaseModel):
    verdict_before: str
    verdict_after: str
    verdict_before_label_zh: str
    verdict_after_label_zh: str
    verdict_changed: bool
    verdict_direction: str = Field(..., description="改善 | 恶化 | 持平")
    max_continuous_exceeded_seconds_before: float
    max_continuous_exceeded_seconds_after: float
    max_continuous_exceeded_seconds_delta: float
    max_exceed_seconds_limit: float
    timeline_change_count: int
    conclusion_zh: str


class RevisionReportOut(BaseModel):
    window: dict[str, Any]
    old_certificate: ReportCertificateRef
    new_certificate: ReportCertificateRef
    rules_diff: ReportRulesDiff
    readings_diff: ReportReadingsDiff
    instrument_faults_diff: ReportInstrumentFaultsDiff = Field(
        ..., description="两证快照有效仪器失准故障时段差异（新登记/撤回/变更）")
    timeline: ReportTimeline
    summary: ReportSummary


# ------------------------------------------------------------ 移交放行单

class HandoverOrderSubmit(BaseModel):
    """馆员办理展柜移交：凭非空移交单号 + 非空窗口标识列表申请放行。

    - 窗口标识列表非空且不得重复（422）；任一窗口不存在（404）；
    - 每个窗口都必须有当前可用证书（最新签发版本未撤销）且结论合格，
      否则整单不予登记（409，不留记录）；
    - 同一单号按窗口集合（不计顺序）重放 -> 返回原单及当前有效状态
      （即使原单已失效）；窗口集合不同 -> 409。
    """

    handover_no: str = Field(..., min_length=1, max_length=100,
                             description="移交单号（非空，不能只含空白字符；全局唯一）")
    window_ids: list[int] = Field(..., min_length=1,
                                  description="非空窗口标识列表（不得重复）")

    @field_validator("handover_no")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("移交单号不能为空（不能只含空白字符）")
        return v


class HandoverOrderItemOut(BaseModel):
    """移交单明细：登记时固化的窗口-证书对应，及查询时的核对结果。"""

    window_id: int
    certificate_id: int = Field(..., description="登记时固化的证书标识")
    certificate_version: int = Field(..., description="登记时固化的证书版本")
    verdict: str = Field(..., description="登记时固化的证书结论")
    verdict_label_zh: str
    certificate_revoked: bool = Field(..., description="查询时该引用证书是否已撤销")
    has_newer_version: bool = Field(..., description="查询时该窗口是否已有更新版本证书")
    item_valid: bool = Field(..., description="该明细当前是否有效（未撤销且无更新版本）")


class HandoverOrderOut(BaseModel):
    handover_no: str
    status: str = Field(..., description="valid | invalid")
    status_label_zh: str = Field(..., description="有效 | 失效")
    created_at: str = Field(..., description="登记时间（UTC）")
    items: list[HandoverOrderItemOut] = Field(
        ..., description="登记时明细（固化，顺序与申请列表一致）+ 查询时逐证核对结果")


class HandoverOrderSubmitResponse(BaseModel):
    effect: str = Field(..., description="created | idempotent")
    order: HandoverOrderOut


class ErrorBody(BaseModel):
    detail: str


# WindowOut.current_certificate 前向引用了后文定义的 CertificateOut，
# 全部模型定义完成后重建一次以解析该引用。
WindowOut.model_rebuild()
# 故障请求模型在 EvaluationOut 之前定义，重建以解析其前向引用。
InstrumentFaultResponse.model_rebuild()
InstrumentFaultWithdrawResponse.model_rebuild()
