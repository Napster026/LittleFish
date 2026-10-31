"""需求驱动、可选情绪偏好的纯活动选择；不执行或修改角色状态。"""

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import ActivityDefinition, ExecutionMode
from .emotion_preferences import ActivityEmotionPreference, EmotionPreferencePolicy
from ..persona.emotion import EmotionState, VAD_NAMES
from ..persona.needs import NEED_NAMES, NeedState


class SelectionPolicy(BaseModel):
    """示例默认值尚未调优；权重为 0 表示不计入该项效用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    urgency_threshold: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 60.0
    energy: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 1.0
    satiety: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 1.0
    boredom: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 1.0
    social_need: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 1.0

    @field_validator("urgency_threshold", *NEED_NAMES, mode="before")
    @classmethod
    def numeric(cls, value):
        if type(value) not in (int, float):
            raise ValueError("选择参数必须是数值")
        return value


@dataclass(frozen=True)
class ActivityChoice:
    definition: ActivityDefinition | None
    scores: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class ActivityEvaluation:
    definition: ActivityDefinition
    score: float
    eligible: bool
    reason: str | None = None
    emotion_adjustment: float = 0.0

    @property
    def final_score(self) -> float:
        return self.score + self.emotion_adjustment


class ActivitySelector:
    """比较完成后的加权需求压力下降量；同分按定义 ID 排序。

    精力和饱食度的压力为 100-value，另两项为 value。至少缓解一项
    达到阈值的有效需求，且总净收益为正才会入选。平方压力使紧迫项
    有更高边际收益；副作用与边界按实际可生效数值计入。
    """

    def __init__(
        self, definitions: tuple[ActivityDefinition, ...], *, policy: SelectionPolicy | None = None,
        emotion_policy: EmotionPreferencePolicy | None = None,
        emotion_preferences: tuple[ActivityEmotionPreference, ...] = (),
    ):
        self.policy = SelectionPolicy.model_validate((policy or SelectionPolicy()).model_dump())
        copies = tuple(ActivityDefinition.model_validate(d.model_dump()) for d in definitions)
        if len({d.definition_id for d in copies}) != len(copies):
            raise ValueError("候选活动定义 ID 不能重复")
        self.definitions = tuple(sorted(copies, key=lambda d: d.definition_id))
        self.emotion_policy = EmotionPreferencePolicy.model_validate(
            (emotion_policy or EmotionPreferencePolicy()).model_dump(),
        )
        preferences = tuple(ActivityEmotionPreference.model_validate(p.model_dump()) for p in emotion_preferences)
        ids = [p.definition_id for p in preferences]
        if len(set(ids)) != len(ids) or not set(ids) <= {d.definition_id for d in copies}:
            raise ValueError("情绪偏好必须关联已知活动，且每个活动只能配置一次")
        self.emotion_preferences = tuple(sorted(preferences, key=lambda p: p.definition_id))
        self._emotion_weights = {p.definition_id: p.weights for p in self.emotion_preferences}

    @property
    def emotion_influence_enabled(self) -> bool:
        return (self.emotion_policy.max_adjustment > 0
                and self.emotion_policy.max_relative_adjustment > 0
                and any(getattr(p.weights, name) != 0
                        for p in self.emotion_preferences for name in VAD_NAMES))

    def evaluate(self, needs: NeedState, *, emotion: EmotionState | None = None) -> tuple[ActivityEvaluation, ...]:
        """提供全部候选的需求评分与不可用原因，供统一仲裁使用。"""
        needs = NeedState.model_validate(needs.model_dump())
        if emotion is not None:
            emotion = EmotionState.model_validate(emotion.model_dump())
        evaluations = []
        for definition in self.definitions:
            effect = definition.completion_needs_delta
            reason = None
            if ExecutionMode.SIMULATED not in definition.supported_modes:
                reason = "unsupported_mode"
            elif definition.estimated_duration_seconds is None:
                reason = "unknown_duration"
            elif effect is None:
                reason = "no_need_effect"
            if reason is not None:
                evaluations.append(ActivityEvaluation(definition, 0.0, False, reason))
                continue
            score, eligible = 0.0, False
            for name in NEED_NAMES:
                value = getattr(needs, name)
                after = min(100.0, max(0.0, value + getattr(effect, name)))
                before_pressure = 100.0 - value if name in ("energy", "satiety") else value
                after_pressure = 100.0 - after if name in ("energy", "satiety") else after
                weight = getattr(self.policy, name)
                eligible |= (weight > 0 and before_pressure >= self.policy.urgency_threshold
                             and after_pressure < before_pressure)
                score += weight * ((before_pressure / 100) ** 2 - (after_pressure / 100) ** 2)
            adjustment = 0.0
            weights = self._emotion_weights.get(definition.definition_id)
            if eligible and score > 0 and weights is not None:
                adjustment = self.emotion_policy.adjustment(score, weights, emotion)
            evaluations.append(ActivityEvaluation(
                definition, score, eligible, None if eligible else "no_urgent_need", adjustment,
            ))
        return tuple(evaluations)

    def choose(self, needs: NeedState, *, emotion: EmotionState | None = None) -> ActivityChoice:
        """按最终分选择；情绪修正默认为零，自动执行仍通过 BehaviorArbiter。"""
        evaluations = self.evaluate(needs, emotion=emotion)
        scores = tuple((e.definition.definition_id, e.final_score) for e in evaluations if e.eligible)
        positive = [e for e in evaluations if e.eligible and e.score > 0 and e.final_score > 0]
        best = min(positive, key=lambda e: (-e.final_score, e.definition.definition_id)) if positive else None
        return ActivityChoice(definition=None if best is None else best.definition, scores=scores)
