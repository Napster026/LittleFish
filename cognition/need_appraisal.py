"""体验对心理需求的有界影响；物理需求始终由执行事实与时间演化负责。"""

from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from ..persona.needs import NeedDelta

Amount = Annotated[float, Field(ge=-10, le=10, allow_inf_nan=False)]


class PsychologicalNeedEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    boredom: Amount = 0.0
    social_need: Amount = 0.0

    @field_validator("boredom", "social_need", mode="before")
    @classmethod
    def numeric(cls, value):
        if type(value) not in (int, float):
            raise ValueError("心理需求权重必须是有限数值")
        return value


class NeedAppraisalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: StrictBool = True
    max_delta: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 2.0
    cooldown_seconds: Annotated[float, Field(ge=.001, le=3600, allow_inf_nan=False)] = 30.0
    chat_positive: Amount = -.5
    chat_negative: Amount = .5
    chat_engaged: Amount = -.3
    interest: PsychologicalNeedEffect = Field(default_factory=lambda: PsychologicalNeedEffect(boredom=-2))
    frustration: PsychologicalNeedEffect = Field(default_factory=lambda: PsychologicalNeedEffect(social_need=.5))
    progress: PsychologicalNeedEffect = Field(default_factory=lambda: PsychologicalNeedEffect(boredom=-.5))
    control: PsychologicalNeedEffect = Field(default_factory=PsychologicalNeedEffect)
    pleasantness: PsychologicalNeedEffect = Field(default_factory=lambda: PsychologicalNeedEffect(social_need=-1))
    activation: PsychologicalNeedEffect = Field(default_factory=PsychologicalNeedEffect)

    @field_validator("max_delta", "cooldown_seconds", "chat_positive", "chat_negative", "chat_engaged",
                     mode="before")
    @classmethod
    def numeric(cls, value):
        if type(value) not in (int, float):
            raise ValueError("需求评价参数必须为有限数值")
        return value

    def bounded(self, values):
        data = {axis: max(-self.max_delta, min(self.max_delta, values.get(axis, 0)))
                for axis in ("boredom", "social_need")}
        return NeedDelta(**data) if any(data.values()) else None


def needs_from_experience(request, experience, policy):
    if not policy.enabled:
        return None, "disabled"
    values = dict.fromkeys(("boredom", "social_need"), 0.0)
    for name in ("interest", "frustration", "progress", "control", "pleasantness", "activation"):
        for axis in values:
            values[axis] += getattr(experience, name)*getattr(getattr(policy, name), axis)
    if not request.context.social_interaction:
        values["social_need"] = 0.0
    masked = any(values.get(axis, 0) != 0 for axis in request.reserved_needs)
    for axis in request.reserved_needs:
        values[axis] = 0.0
    result = policy.bounded(values)
    return result, "completion_owned" if masked else ("experience" if result is not None else "no_need_evidence")
