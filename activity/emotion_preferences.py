"""纯情绪偏好评分配置；不改变活动定义、需求或情绪状态。"""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from ..persona.emotion import EmotionState, VAD, VAD_NAMES


class ActivityEmotionPreference(BaseModel):
    """权重为正偏好该轴较高的情绪，为负偏好该轴较低的情绪。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    definition_id: StrictStr
    weights: VAD

    @field_validator("definition_id")
    @classmethod
    def identity(cls, value: str):
        if not value.strip() or value != value.strip():
            raise ValueError("偏好的活动 ID 不能为空或包含首尾空白")
        return value


class EmotionPreferencePolicy(BaseModel):
    """默认关闭。max_adjustment 为效用单位，比例上限相对于正需求分。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    max_adjustment: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 0.0
    max_relative_adjustment: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 0.5
    current_weight: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 1.0

    @field_validator("max_adjustment", "max_relative_adjustment", "current_weight", mode="before")
    @classmethod
    def numeric(cls, value: Any):
        if type(value) not in (int, float):
            raise ValueError("情绪偏好参数必须是数值")
        return value

    def adjustment(self, need_score: float, weights: VAD, emotion: EmotionState | None) -> float:
        if emotion is None or need_score <= 0 or self.max_adjustment == 0:
            return 0.0
        signal = sum(
            getattr(weights, name) * (
                self.current_weight * getattr(emotion.current, name)
                + (1 - self.current_weight) * getattr(emotion.mood, name)
            ) for name in VAD_NAMES
        ) / 3.0
        limit = min(self.max_adjustment, need_score * self.max_relative_adjustment)
        return limit * max(-1.0, min(1.0, signal))
