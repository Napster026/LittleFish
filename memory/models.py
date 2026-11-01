"""候选、当前记忆和归并结果；权限元数据由宿主绑定。"""

from datetime import datetime, timezone
from typing import Annotated, Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator
from ..core.events import EventProvenance


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: StrictBool = True
    auto_capture: StrictBool = True
    activity_memories_enabled: StrictBool = True
    private_only: StrictBool = True
    merge_window_seconds: Annotated[int, Field(strict=True, ge=0, le=604800)] = 3600
    candidate_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 20
    recall_limit: Annotated[int, Field(strict=True, ge=1, le=5)] = 3
    max_context_chars: Annotated[int, Field(strict=True, ge=80, le=1000)] = 600
    utc_offset_minutes: Annotated[int, Field(strict=True, ge=-720, le=840)] = 480
    recall_half_life_days: Annotated[float, Field(gt=0, le=36500, allow_inf_nan=False)] = 30.0

    @field_validator("recall_half_life_days", mode="before")
    @classmethod
    def numeric_half_life(cls, value):
        if type(value) not in (int,float):
            raise ValueError("召回半衰期必须为数值")
        return value


class MemoryIndexRepairReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repaired: Annotated[int, Field(strict=True, ge=0)] = 0
    skipped: Annotated[int, Field(strict=True, ge=0)] = 0
    batches: Annotated[int, Field(strict=True, ge=0)] = 0
    sample_ids: tuple[StrictStr, ...] = Field(default=(), max_length=5)


class ActivityMemoryContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    activity_id: StrictStr
    definition_id: StrictStr
    name: StrictStr = Field(min_length=1,max_length=80)
    checkpoint_id: StrictStr | None = Field(default=None,min_length=1,max_length=200)
    phase: Literal["stage", "completed", "failed"]
    task: Literal["success", "partial", "failure", "neutral"]

    @field_validator("activity_id","definition_id","name","checkpoint_id")
    @classmethod
    def text_fields(cls,value):
        if value is not None and (not value.strip() or value!=value.strip()):
            raise ValueError("活动经历标识和名称不能留空或含首尾空白")
        return value

    @model_validator(mode="after")
    def stage_identity(self):
        if self.phase=="stage" and self.checkpoint_id is None:
            raise ValueError("阶段经历必须包含 checkpoint")
        return self


class MemoryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    kind: Literal["episodic", "story"] = "episodic"
    visibility: Literal["self", "scope", "global"] = "scope"
    scope_id: StrictStr | None
    speaker_id: StrictStr | None = None
    about_id: StrictStr | None = None
    claim_type: Literal["identity", "preference", "plan", "event", "commitment", "fact"]
    occurred_at: AwareDatetime
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    target_start_at: AwareDatetime | None = None
    target_end_at: AwareDatetime | None = None
    time_precision: Literal["unknown", "day", "range", "instant"] = "unknown"
    plan_status: Literal["planned", "cancelled", "completed"] | None = None
    text: StrictStr = Field(min_length=1, max_length=2000)
    keywords: tuple[StrictStr, ...] = Field(default=(), max_length=4)
    source_event_id: StrictStr
    provenance: EventProvenance
    importance: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = .5
    draft_key: StrictStr = "primary"
    topic_key: StrictStr = ""
    detail_key: StrictStr = ""
    update_hint: Literal["assert", "correct", "cancel", "complete"] = "assert"
    activity: ActivityMemoryContext | None = None

    @field_validator("occurred_at", "valid_from", "valid_until", "target_start_at", "target_end_at", mode="before")
    @classmethod
    def time_value(cls, value):
        if value is not None and not isinstance(value, (datetime, str)):
            raise ValueError("记忆时间必须为带时区时间或 ISO 字符串")
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @field_validator("occurred_at", "valid_from", "valid_until", "target_start_at", "target_end_at")
    @classmethod
    def utc(cls, value):
        return None if value is None else value.astimezone(timezone.utc)

    @field_validator("text", "scope_id", "speaker_id", "about_id", "source_event_id", "draft_key")
    @classmethod
    def nonempty(cls, value):
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("记忆文本和身份不能留空或含首尾空白")
        return value

    @field_validator("importance", mode="before")
    @classmethod
    def number(cls, value):
        if type(value) not in (float, int):
            raise ValueError("重要度必须为数值")
        return value

    @field_validator("keywords")
    @classmethod
    def words(cls, values):
        if any(not w.strip() or w != w.strip() or len(w) > 32 for w in values):
            raise ValueError("关键词必须为 1 到 32 字符的非空文本")
        return tuple(dict.fromkeys(w.casefold() for w in values))

    @model_validator(mode="after")
    def consistency(self):
        if self.visibility == "scope" and self.scope_id is None:
            raise ValueError("会话记忆必须包含 scope_id")
        if self.visibility == "self" and self.scope_id is not None:
            raise ValueError("内部经历不能带公开会话范围")
        if self.activity is not None and self.claim_type!="event":
            raise ValueError("活动经历必须使用 event 类型")
        if self.valid_from is not None and self.valid_until is not None and self.valid_from >= self.valid_until:
            raise ValueError("记忆有效区间必须递增")
        if (self.target_start_at is None) != (self.target_end_at is None):
            raise ValueError("目标时间必须提供完整区间")
        if self.target_start_at is not None and self.target_start_at >= self.target_end_at:
            raise ValueError("目标区间必须递增，结束边界不包含")
        if (self.time_precision == "unknown") != (self.target_start_at is None):
            raise ValueError("时间精度与目标区间不一致")
        if self.claim_type == "plan" and self.plan_status is None:
            raise ValueError("计划需要显式状态")
        if self.claim_type != "plan" and self.plan_status is not None:
            raise ValueError("只有计划可以设置计划状态")
        if len(self.topic_key) > 200 or len(self.detail_key) > 400 or len(self.draft_key) > 80:
            raise ValueError("记忆归并键过长")
        return self


class MemoryRecord(MemoryDraft):
    memory_id: StrictStr
    agent_id: StrictStr
    revision: Annotated[int, Field(strict=True, ge=1)]
    created_at: AwareDatetime  # 首条来源时间；本模块不混入另一套落库墙钟。
    updated_at: AwareDatetime  # 最近关联的来源时间；正文来源仍由 occurred_at 表示。

    @model_validator(mode="after")
    def record_times(self):
        if self.created_at>self.updated_at or self.occurred_at>self.updated_at:
            raise ValueError("记忆创建/陈述时间不能晚于最后更新")
        if not self.agent_id.strip() or not self.memory_id.strip():
            raise ValueError("角色与记忆 ID 不能为空")
        return self


class MemoryWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["created", "duplicate", "enriched", "corrected", "cancelled", "completed", "ignored", "deleted"]
    memory_id: StrictStr | None = None
    revision: Annotated[int, Field(strict=True, ge=1)] | None = None
