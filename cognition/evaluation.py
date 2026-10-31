"""规则、可选体验评价与幅度配置；本模块不发起任何模型调用。"""

from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator
from ..persona.emotion import EmotionImpulse
from .semantic import SemanticSettings
from .need_appraisal import NeedAppraisalPolicy
from ..persona.needs import NeedDelta


class ChatRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rule_id: StrictStr = Field(min_length=1, max_length=64)
    phrases: tuple[Annotated[StrictStr, Field(min_length=1, max_length=64)], ...] = Field(min_length=1, max_length=16)
    impulse: EmotionImpulse
    priority: Annotated[int, Field(strict=True, ge=0, le=100)] = 10

    @field_validator("rule_id")
    @classmethod
    def identity(cls, value):
        if value != value.strip():
            raise ValueError("规则 ID 不能含首尾空白")
        return value

    @field_validator("phrases")
    @classmethod
    def phrases_valid(cls, values):
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("规则短语不能为空白或重复")
        return values


def default_chat_rules():
    return tuple(ChatRule(rule_id=key, phrases=phrases, impulse=EmotionImpulse(
        valence=v, arousal=a, dominance=d), priority=priority) for key, phrases, v, a, d, priority in (
        ("thanks", ("谢谢你", "感谢你", "多谢你", "辛苦你了"), .03, -.01, .01, 20),
        ("praise", ("你真棒", "你很棒", "你真厉害", "你很好"), .05, .02, .02, 30),
        ("insult", ("你是笨蛋", "你这个笨蛋", "你真没用", "你是废物", "讨厌你"), -.05, .03, -.02, 40),
        ("apology", ("对不起", "抱歉"), .02, -.02, .01, 25),
        ("invitation", ("一起聊聊", "陪我聊聊", "和我聊天"), .02, .02, 0, 10),
        ("friendly_emoji", ("😊", "🥰"), .015, 0, 0, 1),
    ))


class ActivityRuleEffects(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage_success: EmotionImpulse | None = Field(default_factory=lambda: EmotionImpulse(valence=.03, arousal=0, dominance=.02))
    interesting_content: EmotionImpulse | None = Field(default_factory=lambda: EmotionImpulse(valence=.06, arousal=.03, dominance=0))
    stage_failure: EmotionImpulse | None = Field(default_factory=lambda: EmotionImpulse(valence=-.04, arousal=.02, dominance=-.02))
    activity_completed: EmotionImpulse | None = Field(default_factory=lambda: EmotionImpulse(valence=.04, arousal=-.01, dominance=.02))
    activity_failed: EmotionImpulse | None = Field(default_factory=lambda: EmotionImpulse(valence=-.06, arousal=.03, dominance=-.03))


class EvaluationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: StrictBool = True
    activity_feedback_enabled: StrictBool = True
    cooldown_seconds: Annotated[float, Field(ge=0, le=3600, allow_inf_nan=False)] = 30.0
    max_axis_delta: Annotated[float, Field(gt=0, le=.3, allow_inf_nan=False)] = .1
    max_effect_age_seconds: Annotated[float, Field(ge=1, le=3600, allow_inf_nan=False)] = 300.0
    max_message_chars: Annotated[int, Field(strict=True, ge=1, le=8000)] = 2000
    chat_rules: tuple[ChatRule, ...] = Field(default_factory=default_chat_rules, max_length=64)
    activity_effects: ActivityRuleEffects = Field(default_factory=ActivityRuleEffects)
    semantic: SemanticSettings = Field(default_factory=SemanticSettings)
    needs: NeedAppraisalPolicy = Field(default_factory=NeedAppraisalPolicy)

    @field_validator("cooldown_seconds", "max_axis_delta", "max_effect_age_seconds", mode="before")
    @classmethod
    def numeric(cls, value):
        if type(value) not in (int, float):
            raise ValueError("本地评价参数必须为数值")
        return value

    @model_validator(mode="after")
    def unique_rules(self):
        if 0 < self.cooldown_seconds < .001:
            raise ValueError("冷却间隔必须为零或至少 0.001 秒")
        ids = [r.rule_id for r in self.chat_rules]
        if len(set(ids)) != len(ids):
            raise ValueError("聊天规则 ID 不能重复")
        return self

    def bounded(self, impulse: EmotionImpulse | None) -> EmotionImpulse | None:
        if impulse is None:
            return None
        return EmotionImpulse(**{name: max(-self.max_axis_delta, min(self.max_axis_delta, getattr(impulse, name)))
                                 for name in ("valence", "arousal", "dominance")})


class RuleEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rule_id: StrictStr | None = None
    reason: StrictStr
    impulse: EmotionImpulse | None = None
    needs_delta: NeedDelta | None = None
    needs_reason: StrictStr | None = None

    @field_validator("needs_delta")
    @classmethod
    def psychological_only(cls, value):
        if value is not None and (value.energy != 0 or value.satiety != 0):
            raise ValueError("评价只允许心理需求变化，精力和饱食须由执行事实决定")
        return value
