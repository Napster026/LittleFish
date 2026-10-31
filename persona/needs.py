"""需求模型与自然时间演化；计算不访问数据库、调度器或活动执行器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


NeedValue = Annotated[float, Field(ge=0.0, le=100.0, allow_inf_nan=False)]
NeedDeltaValue = Annotated[float, Field(allow_inf_nan=False)]
NEED_NAMES = ("energy", "satiety", "boredom", "social_need")


def parse_need_time(value: Any) -> datetime | None:
    """需求时间字段只接受明确的带时区时间，不接受数字时间戳。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("需求时间必须带时区")
    return value.astimezone(timezone.utc)


# noinspection PyNestedDecorators
class NeedState(BaseModel):
    """四项需求的只读数据模型，创建时必须明确提供所有初始值。

    energy、satiety 越高表示越充足；boredom、social_need 越高表示
    越需要活动或社交来满足。取值范围均为 0～100，越界时拒绝输入，
    不在模型层静默截断。具体变化速率和初始值由后续策略决定。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    energy: NeedValue
    satiety: NeedValue
    boredom: NeedValue
    social_need: NeedValue

    @field_validator("energy", "satiety", "boredom", "social_need", mode="before")
    @classmethod
    def validate_numeric_input(cls, value: Any) -> Any:
        """只接受整数或浮点数，拒绝布尔值和数值字符串。"""
        if type(value) not in (int, float):
            raise ValueError("需求值必须是整数或浮点数，不能是布尔值或字符串")
        return value


# noinspection PyNestedDecorators
class NeedDelta(BaseModel):
    """需求变化量；正数表示增加，负数表示减少，未提供的项为 0。

    至少一项必须非零。这里只检查数值类型与有限性；变化量不是最终状态，
    不沿用 NeedState 的 0～100 范围。单次变化限制与最终状态限幅由后续
    结算策略处理。正负变化的实际含义与 NeedState 四项需求的方向一致。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    energy: NeedDeltaValue = 0.0
    satiety: NeedDeltaValue = 0.0
    boredom: NeedDeltaValue = 0.0
    social_need: NeedDeltaValue = 0.0

    @field_validator("energy", "satiety", "boredom", "social_need", mode="before")
    @classmethod
    def validate_numeric_input(cls, value: Any) -> Any:
        """拒绝把布尔值、数值字符串等隐式转换成变化量。"""
        if type(value) not in (int, float):
            raise ValueError("需求变化量必须是整数或浮点数")
        return value

    @model_validator(mode="after")
    def validate_nonzero_delta(self) -> NeedDelta:
        """检查四项中至少一项有变化，不能按总和判断。"""
        if not any(
            value != 0.0
            for value in (self.energy, self.satiety, self.boredom, self.social_need)
        ):
            raise ValueError("需求变化量至少有一项非零")
        return self


# noinspection PyNestedDecorators
class NeedRates(BaseModel):
    """每小时的自然变化量，必须显式配置；不包括活动完成效果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    energy: Annotated[float, Field(le=0, allow_inf_nan=False)]
    satiety: Annotated[float, Field(le=0, allow_inf_nan=False)]
    boredom: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    social_need: Annotated[float, Field(ge=0, allow_inf_nan=False)]

    @field_validator(*NEED_NAMES, mode="before")
    @classmethod
    def validate_rate(cls, value: Any) -> Any:
        if type(value) not in (int, float):
            raise ValueError("自然变化速率必须是数值，不能是布尔值或字符串")
        return value


# noinspection PyNestedDecorators
class NeedTimeAdvance(BaseModel):
    """候选时间游标变化；from_at=None 仅用于首次建立基线。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_at: AwareDatetime | None
    to_at: AwareDatetime

    @field_validator("from_at", "to_at", mode="before")
    @classmethod
    def validate_time(cls, value: Any) -> datetime | None:
        return parse_need_time(value)

    @model_validator(mode="after")
    def validate_order(self) -> NeedTimeAdvance:
        if self.from_at is not None and self.to_at <= self.from_at:
            raise ValueError("需求时间游标必须向前推进")
        return self


@dataclass(frozen=True)
class NeedUpdate:
    time: NeedTimeAdvance
    delta: NeedDelta | None


class NeedEngine:
    """按经过时长计算自然变化，返回候选变化及时间窗口，不修改输入。"""

    def __init__(self, rates: NeedRates):
        self.rates = NeedRates.model_validate(rates.model_dump())

    def advance(
        self, needs: NeedState, *, from_at: datetime | None, to_at: datetime,
    ) -> NeedUpdate | None:
        needs = NeedState.model_validate(needs.model_dump())
        start, end = parse_need_time(from_at), parse_need_time(to_at)
        if end is None:
            raise ValueError("必须提供需求计算结束时间")
        if start == end:
            return None
        window = NeedTimeAdvance(from_at=start, to_at=end)
        if start is None:
            return NeedUpdate(time=window, delta=None)
        hours = (end - start).total_seconds() / 3600.0
        changes = {}
        for name in NEED_NAMES:
            rate, current = getattr(self.rates, name), getattr(needs, name)
            # 最多需要跨越整个取值范围，也避免超大速率产生非有限变化量。
            magnitude = min(100.0, abs(rate) * hours)
            value = max(0.0, current - magnitude) if rate < 0 else min(100.0, current + magnitude)
            changes[name] = value - current
        delta = NeedDelta(**changes) if any(changes.values()) else None
        return NeedUpdate(time=window, delta=delta)
