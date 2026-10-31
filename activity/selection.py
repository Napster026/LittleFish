"""需求驱动的纯活动选择；不启动执行器或修改角色状态。"""

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from activity.models import ActivityDefinition, ExecutionMode
from persona.needs import NEED_NAMES, NeedState


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


class ActivitySelector:
    """比较完成后的加权需求压力下降量；同分按定义 ID 排序。

    精力和饱食度的压力为 100-value，另两项为 value。至少缓解一项
    达到阈值的有效需求，且总净收益为正才会入选。平方压力使紧迫项
    有更高边际收益；副作用与边界按实际可生效数值计入。
    """

    def __init__(self, definitions: tuple[ActivityDefinition, ...], *, policy: SelectionPolicy | None = None):
        self.policy = SelectionPolicy.model_validate((policy or SelectionPolicy()).model_dump())
        copies = tuple(ActivityDefinition.model_validate(d.model_dump()) for d in definitions)
        if len({d.definition_id for d in copies}) != len(copies):
            raise ValueError("候选活动定义 ID 不能重复")
        self.definitions = tuple(sorted(copies, key=lambda d: d.definition_id))

    def evaluate(self, needs: NeedState) -> tuple[ActivityEvaluation, ...]:
        """提供全部候选的需求评分与不可用原因，供统一仲裁使用。"""
        needs = NeedState.model_validate(needs.model_dump())
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
            evaluations.append(ActivityEvaluation(
                definition, score, eligible, None if eligible else "no_urgent_need",
            ))
        return tuple(evaluations)

    def choose(self, needs: NeedState) -> ActivityChoice:
        """保留原选择接口与分数语义；自动执行入口改为通过 BehaviorArbiter。"""
        evaluations = self.evaluate(needs)
        scores = tuple((e.definition.definition_id, e.score) for e in evaluations if e.eligible)
        positive = [e for e in evaluations if e.eligible and e.score > 0]
        best = min(positive, key=lambda e: (-e.score, e.definition.definition_id)) if positive else None
        return ActivityChoice(definition=None if best is None else best.definition, scores=scores)
