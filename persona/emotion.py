"""VAD 情绪、心情和稳定基线；纯计算，不依赖内核、活动或数据库。"""

import math
from datetime import datetime
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from persona.needs import parse_need_time


VAD_NAMES = ("valence", "arousal", "dominance")
VADValue = Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]


class VAD(BaseModel):
    """有符号归一化坐标：愉悦度、唤醒度、掌控感，全部为 -1～1。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    valence: VADValue
    arousal: VADValue
    dominance: VADValue

    @field_validator(*VAD_NAMES, mode="before")
    @classmethod
    def numeric(cls, value: Any):
        if type(value) not in (int, float):
            raise ValueError("VAD 必须是数值，不能是布尔值或字符串")
        return value


class EmotionImpulse(VAD):
    """一次经过评价的有界冲击；正负分别表示增加和减少对应维度。"""

    @model_validator(mode="after")
    def nonzero(self):
        if not any(getattr(self, name) != 0 for name in VAD_NAMES):
            raise ValueError("情绪冲击至少有一项非零")
        return self


class EmotionDynamics(BaseModel):
    """时间单位为秒。瞬时情绪的半衰期必须小于心情半衰期。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    emotion_half_life_seconds: Annotated[float, Field(ge=0.001, le=31536000, allow_inf_nan=False)]
    mood_half_life_seconds: Annotated[float, Field(ge=0.001, le=31536000, allow_inf_nan=False)]
    mood_impulse_ratio: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    tick_interval_seconds: Annotated[float, Field(ge=0.001, le=86400, allow_inf_nan=False)] = 60.0

    @field_validator("emotion_half_life_seconds", "mood_half_life_seconds", "mood_impulse_ratio",
                     "tick_interval_seconds", mode="before")
    @classmethod
    def numeric(cls, value: Any):
        if type(value) not in (int, float):
            raise ValueError("情绪参数必须是数值")
        return value

    @model_validator(mode="after")
    def order(self):
        if self.emotion_half_life_seconds >= self.mood_half_life_seconds:
            raise ValueError("瞬时情绪必须比心情回归得更快")
        return self


class EmotionState(BaseModel):
    """规则与坐标一并持久化；updated_at 是独立的情绪计算游标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    current: VAD
    mood: VAD
    baseline: VAD
    dynamics: EmotionDynamics
    updated_at: AwareDatetime

    @field_validator("updated_at", mode="before")
    @classmethod
    def time(cls, value: Any):
        parsed = parse_need_time(value)
        if parsed is None:
            raise ValueError("情绪时间不能为空")
        return parsed


class EmotionChange(BaseModel):
    """候选新快照及其依据的旧游标；None 仅用于显式初始化。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    from_at: AwareDatetime | None
    state: EmotionState

    @field_validator("from_at", mode="before")
    @classmethod
    def time(cls, value: Any):
        return parse_need_time(value)

    @model_validator(mode="after")
    def order(self):
        if self.from_at is not None and self.state.updated_at < self.from_at:
            raise ValueError("情绪时间不能倒退")
        return self


def _bounded(value: float) -> float:
    return max(-1.0, min(1.0, value))


class EmotionEngine:
    """解析求解耦合回归：E'=a(M-E)，M'=b(B-M)。

    同样经过时长，分段计算和一次计算只存在浮点误差。基线和动力参数
    不随推进或冲击改变。事件在处理时生效，不追溯改写历史状态。
    """

    def advance(self, state: EmotionState, *, to_at: datetime) -> EmotionChange | None:
        state = EmotionState.model_validate(state.model_dump())
        end = parse_need_time(to_at)
        if end is None or end < state.updated_at:
            raise ValueError("情绪推进时间必须存在且不能倒退")
        if end == state.updated_at:
            return None
        seconds = (end - state.updated_at).total_seconds()
        a = math.log(2) / state.dynamics.emotion_half_life_seconds
        b = math.log(2) / state.dynamics.mood_half_life_seconds
        ea, eb = math.exp(-a * seconds), math.exp(-b * seconds)
        # 近似相等的速率用 expm1 避免两个指数相减的精度损失。
        coupled = a * seconds * ea if a == b else a * eb * (-math.expm1(-(a - b) * seconds)) / (a - b)
        current, mood = {}, {}
        for name in VAD_NAMES:
            baseline = getattr(state.baseline, name)
            mood_offset = getattr(state.mood, name) - baseline
            mood[name] = _bounded(baseline + mood_offset * eb)
            current[name] = _bounded(
                baseline + (getattr(state.current, name) - baseline) * ea + mood_offset * coupled,
            )
        return EmotionChange(from_at=state.updated_at, state=EmotionState(
            **{**state.model_dump(), "current": current, "mood": mood, "updated_at": end},
        ))

    def apply_impulse(
        self, state: EmotionState, impulse: EmotionImpulse, *, at: datetime,
    ) -> EmotionChange:
        state = EmotionState.model_validate(state.model_dump())
        impulse = EmotionImpulse.model_validate(impulse.model_dump())
        advanced = self.advance(state, to_at=at)
        before = state if advanced is None else advanced.state
        current, mood = {}, {}
        for name in VAD_NAMES:
            delta = getattr(impulse, name)
            current[name] = _bounded(getattr(before.current, name) + delta)
            mood[name] = _bounded(getattr(before.mood, name) + delta * before.dynamics.mood_impulse_ratio)
        return EmotionChange(from_at=state.updated_at, state=EmotionState(
            **{**before.model_dump(), "current": current, "mood": mood},
        ))
