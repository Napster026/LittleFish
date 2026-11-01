"""关系是当前角色对互动的有限估计，不是情绪、信任或权限。"""

from datetime import date, datetime, timezone
from typing import Annotated, Literal
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator


class RelationshipSettings(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    enabled: StrictBool=True
    private_only: StrictBool=True
    cooldown_seconds: Annotated[float,Field(ge=0,le=86400,allow_inf_nan=False)]=300.0
    max_effect_age_seconds: Annotated[float,Field(ge=1,le=3600,allow_inf_nan=False)]=300.0
    familiarity_step: Annotated[float,Field(ge=0,le=1,allow_inf_nan=False)]=.2
    positive_affinity_step: Annotated[float,Field(ge=0,le=1,allow_inf_nan=False)]=.1
    negative_affinity_step: Annotated[float,Field(ge=0,le=1,allow_inf_nan=False)]=.2
    daily_familiarity_limit: Annotated[float,Field(ge=0,le=100,allow_inf_nan=False)]=2.0
    daily_affinity_limit: Annotated[float,Field(ge=0,le=100,allow_inf_nan=False)]=1.0
    utc_offset_minutes: Annotated[int,Field(strict=True,ge=-720,le=840)]=480
    affinity_regression_enabled: StrictBool=True
    affinity_settlement_hour: Annotated[int,Field(strict=True,ge=0,le=23)]=4
    affinity_daily_retention: Annotated[float,Field(ge=0,le=1,allow_inf_nan=False)]=.5
    affinity_negative_recovery: Annotated[float,Field(ge=0,le=100,allow_inf_nan=False)]=.1
    affinity_positive_baseline: Annotated[float,Field(ge=0,le=100,allow_inf_nan=False)]=.5
    positive_rule_ids: tuple[StrictStr,...]=Field(default=("thanks","praise","apology","invitation","friendly_emoji"),max_length=64)
    negative_rule_ids: tuple[StrictStr,...]=Field(default=("insult",),max_length=64)

    @field_validator("cooldown_seconds","max_effect_age_seconds","familiarity_step","positive_affinity_step",
                     "negative_affinity_step","daily_familiarity_limit","daily_affinity_limit",
                     "affinity_daily_retention","affinity_negative_recovery","affinity_positive_baseline",mode="before")
    @classmethod
    def number(cls,value):
        if type(value) not in (int,float):
            raise ValueError("关系参数必须为有限数值")
        return value

    @field_validator("positive_rule_ids","negative_rule_ids")
    @classmethod
    def rules(cls,values):
        if len(values)!=len(set(values)) or any(not v.strip() or v!=v.strip() or len(v)>64 for v in values):
            raise ValueError("关系规则 ID 必须非空、唯一且最多 64 字符")
        return values

    @model_validator(mode="after")
    def disjoint_rules(self):
        if set(self.positive_rule_ids)&set(self.negative_rule_ids):
            raise ValueError("关系正负规则不能重叠")
        return self


class RelationshipSignal(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True)
    polarity: Literal["positive","negative","neutral"]="neutral"
    rule_id: StrictStr | None=None


class RelationshipProfile(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True,allow_inf_nan=False)
    agent_id: StrictStr
    platform_id: StrictStr
    user_id: StrictStr
    familiarity: Annotated[float,Field(ge=0,le=100)]=0
    affinity: Annotated[float,Field(ge=-100,le=100)]=0
    last_interaction_at: AwareDatetime
    cooldown_until: AwareDatetime | None=None
    budget_day: StrictStr
    affinity_day: StrictStr | None=None
    familiarity_used: Annotated[float,Field(ge=0,le=100)]=0
    affinity_used: Annotated[float,Field(ge=0,le=100)]=0
    revision: Annotated[int,Field(strict=True,ge=1)]
    updated_at: AwareDatetime

    @field_validator("agent_id","platform_id","user_id")
    @classmethod
    def identifier(cls,value):
        if not value.strip() or value!=value.strip():
            raise ValueError("关系身份不能留空或包含首尾空白")
        return value

    @field_validator("familiarity","affinity","familiarity_used","affinity_used",mode="before")
    @classmethod
    def number(cls,value):
        if type(value) not in (int,float):
            raise ValueError("关系数值必须为有限数值")
        return value

    @field_validator("last_interaction_at","cooldown_until","updated_at",mode="before")
    @classmethod
    def time_input(cls,value):
        if value is not None and not isinstance(value,(datetime,str)):
            raise ValueError("关系时间必须为带时区时间或 ISO 字符串")
        return datetime.fromisoformat(value.replace("Z","+00:00")) if isinstance(value,str) else value

    @field_validator("last_interaction_at","cooldown_until","updated_at")
    @classmethod
    def utc(cls,value):
        return None if value is None else value.astimezone(timezone.utc)

    @field_validator("budget_day","affinity_day")
    @classmethod
    def day(cls,value):
        if value is None:
            return None
        if date.fromisoformat(value).isoformat()!=value:
            raise ValueError("关系预算日期格式必须是 YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def times(self):
        if self.last_interaction_at>self.updated_at:
            raise ValueError("最近互动不能晚于档案更新时间")
        return self

    @property
    def actor_id(self):
        return f"{self.platform_id}:user:{self.user_id}"


class RelationshipChange(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True,allow_inf_nan=False)
    agent_id: StrictStr
    source_event_id: StrictStr
    scope_id: StrictStr
    platform_id: StrictStr | None
    user_id: StrictStr | None
    occurred_at: AwareDatetime
    at: AwareDatetime
    rule_id: StrictStr | None=None
    polarity: Literal["positive","negative","neutral"]="neutral"
    reason: Literal["updated","cooldown","daily_limit","at_boundary","out_of_order","disabled",
        "private_only","not_directed","empty_message","command","unknown_identity","self_message",
        "expired","history_not_backfilled","daily_regression"]
    familiarity_before: float=0
    familiarity_after: float=0
    affinity_before: float=0
    affinity_after: float=0
    profile_revision: Annotated[int,Field(strict=True,ge=1)] | None=None
    kind: Literal["interaction","regression"]="interaction"
    from_day: StrictStr | None=None
    to_day: StrictStr | None=None
    days: Annotated[int,Field(strict=True,ge=0)]=0

    @model_validator(mode="after")
    def regression_metadata(self):
        if self.kind=="regression":
            if (self.scope_id!="" or self.reason!="daily_regression" or self.platform_id is None or self.user_id is None
                    or self.from_day is None or self.to_day is None or self.days<1
                    or (date.fromisoformat(self.to_day)-date.fromisoformat(self.from_day)).days!=self.days
                    or self.familiarity_before!=self.familiarity_after):
                raise ValueError("日回归回执必须是当前用户的无会话数值维护")
        elif self.reason=="daily_regression" or not self.scope_id.strip() or self.days or self.from_day is not None or self.to_day is not None:
            raise ValueError("互动回执不能伪装成无范围日回归")
        return self

    @property
    def familiarity_delta(self):
        return round(self.familiarity_after-self.familiarity_before,10)

    @property
    def affinity_delta(self):
        return round(self.affinity_after-self.affinity_before,10)
