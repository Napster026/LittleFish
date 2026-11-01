"""主动表达的确定性策略与持久化意图；不调用模型或平台。"""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator
from ..core.scheduler import normalize_schedule_time


class ExpressionTarget(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    platform_id: StrictStr
    user_id: StrictStr

    @field_validator("platform_id","user_id")
    @classmethod
    def identity(cls,value):
        if not value.strip() or value!=value.strip() or ":" in value:
            raise ValueError("主动表达目标标识须非空、无首尾空白及冒号")
        return value

    @property
    def scope_id(self):
        return f"{self.platform_id}:FriendMessage:{self.user_id}"


class ExpressionSettings(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    enabled: StrictBool=True
    send_enabled: StrictBool=False
    targets: tuple[ExpressionTarget,...]=Field(default=(),max_length=32)
    social_threshold: Annotated[StrictInt,Field(ge=1,le=100)]=70
    cooldown_seconds: Annotated[StrictInt,Field(ge=60,le=604800)]=21600
    daily_limit: Annotated[StrictInt,Field(ge=1,le=10)]=2
    min_silence_seconds: Annotated[StrictInt,Field(ge=30,le=86400)]=1800
    max_interaction_age_seconds: Annotated[StrictInt,Field(ge=60,le=2592000)]=604800
    intent_ttl_seconds: Annotated[StrictInt,Field(ge=10,le=600)]=120
    generation_timeout_seconds: Annotated[StrictInt,Field(ge=1,le=120)]=30
    send_timeout_seconds: Annotated[StrictInt,Field(ge=1,le=60)]=15
    quiet_start_hour: Annotated[StrictInt,Field(ge=0,le=23)]=23
    quiet_end_hour: Annotated[StrictInt,Field(ge=0,le=23)]=8
    utc_offset_minutes: Annotated[StrictInt,Field(ge=-720,le=840)]=480
    max_body_chars: Annotated[StrictInt,Field(ge=20,le=500)]=160

    @model_validator(mode="after")
    def unique_targets(self):
        if len({t.scope_id for t in self.targets})!=len(self.targets):
            raise ValueError("主动表达目标不能重复")
        if self.max_interaction_age_seconds<self.min_silence_seconds:
            raise ValueError("最近互动有效期不能小于最短静默期")
        return self


class ExpressionIntent(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    intent_id: StrictStr
    agent_id: StrictStr
    target: ExpressionTarget
    source_event_id: StrictStr
    created_at: datetime
    expires_at: datetime
    updated_at: datetime
    mode: Literal["preview","send"]
    status: Literal["preview","pending","generating","sending","sent","failed","cancelled","unknown"]
    body: StrictStr=Field(default="",max_length=500)
    reason: StrictStr="social_need"

    @field_validator("created_at","expires_at","updated_at")
    @classmethod
    def aware(cls,value):
        return normalize_schedule_time(value)


class ExpressionHead(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    agent_id: StrictStr
    revision: Annotated[StrictInt,Field(ge=1)]
    budget_day: StrictStr
    used: Annotated[StrictInt,Field(ge=0)]
    last_attempt_at: datetime
    intent: ExpressionIntent

    @field_validator("last_attempt_at")
    @classmethod
    def aware(cls,value):return normalize_schedule_time(value)


def local_day(now,settings):
    return normalize_schedule_time(now).astimezone(timezone(timedelta(minutes=settings.utc_offset_minutes))).date().isoformat()


def quiet_time(now,settings):
    hour=normalize_schedule_time(now).astimezone(timezone(timedelta(minutes=settings.utc_offset_minutes))).hour
    a,b=settings.quiet_start_hour,settings.quiet_end_hour
    return False if a==b else (a<=hour<b if a<b else hour>=a or hour<b)


def intent_busy(intent,now):
    return intent is not None and intent.status in ("pending","generating","sending") and now<intent.expires_at


def gate(settings,now,head=None):
    if not settings.enabled:return "disabled"
    if quiet_time(now,settings):return "quiet_hours"
    if head is not None:
        if now<head.last_attempt_at:return "clock_reversed"
        if intent_busy(head.intent,now):return "expression_pending"
        if (now-head.last_attempt_at).total_seconds()<settings.cooldown_seconds:return "cooldown"
        if head.budget_day==local_day(now,settings) and head.used>=settings.daily_limit:return "daily_limit"
    return None
