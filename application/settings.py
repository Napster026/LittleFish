"""AstrBot 配置到严格业务参数的边界；默认值仅供首次 QQ 联调。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator

from ..activity.emotion_preferences import ActivityEmotionPreference, EmotionPreferencePolicy
from ..activity.models import ActivityDefinition, ExecutionMode
from ..activity.selection import ActivitySelector, SelectionPolicy
from ..core.arbiter import ArbitrationPolicy
from ..core.state import AgentState
from ..persona.emotion import EmotionDynamics, EmotionState, VAD
from ..persona.needs import NeedRates, NeedState
from ..cognition.evaluation import EvaluationSettings
from ..cognition.prompt_builder import ChatContextSettings
from ..memory.models import MemorySettings
from ..social.models import RelationshipSettings


class InitialEmotion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    current: VAD
    mood: VAD
    baseline: VAD
    dynamics: EmotionDynamics

    def at(self, now) -> EmotionState:
        return EmotionState(**self.model_dump(), updated_at=now)


def default_emotion():
    zero = VAD(valence=0, arousal=0, dominance=0)
    return InitialEmotion(current=zero, mood=zero, baseline=zero, dynamics=EmotionDynamics(
        emotion_half_life_seconds=300, mood_half_life_seconds=7200, mood_impulse_ratio=0.1,
    ))


def default_activities():
    return tuple(ActivityDefinition(
        definition_id=identifier, name=name, supported_modes=["simulated"],
        estimated_duration_seconds=duration, completion_needs_delta=effects,
    ) for identifier, name, duration, effects in (
        ("rest", "休息", 600, {"energy": 35}),
        ("eat", "进食", 300, {"satiety": 40}),
        ("play", "娱乐", 300, {"boredom": -40, "energy": -5}),
        ("social", "模拟社交", 300, {"social_need": -35, "energy": -5}),
    ))


class SimulationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    initial_needs: NeedState = Field(default_factory=lambda: NeedState(energy=40, satiety=70, boredom=30, social_need=50))
    need_rates: NeedRates = Field(default_factory=lambda: NeedRates(energy=-4, satiety=-6, boredom=5, social_need=3))
    needs_interval_seconds: Annotated[float, Field(ge=0.001, le=86400, allow_inf_nan=False)] = 60.0
    activity_interval_seconds: Annotated[float, Field(ge=0.001, le=86400, allow_inf_nan=False)] = 5.0
    initial_emotion: InitialEmotion | None = Field(default_factory=default_emotion)
    activities: tuple[ActivityDefinition, ...] = Field(default_factory=default_activities)
    selection_policy: SelectionPolicy = Field(default_factory=SelectionPolicy)
    emotion_policy: EmotionPreferencePolicy = Field(default_factory=EmotionPreferencePolicy)
    emotion_preferences: tuple[ActivityEmotionPreference, ...] = ()

    @field_validator("needs_interval_seconds", "activity_interval_seconds", mode="before")
    @classmethod
    def numeric(cls, value):
        if type(value) not in (int, float):
            raise ValueError("检查间隔必须是数值")
        return value

    @model_validator(mode="after")
    def validate_catalog(self):
        self.selector()
        if any(ExecutionMode.SIMULATED not in d.supported_modes or d.estimated_duration_seconds is None
               for d in self.activities):
            raise ValueError("当前插件目录只接入有明确时长的模拟活动")
        return self

    def selector(self) -> ActivitySelector:
        return ActivitySelector(self.activities, policy=self.selection_policy,
                                emotion_policy=self.emotion_policy, emotion_preferences=self.emotion_preferences)


class HostSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_id: StrictStr = "persona-001"
    autonomous_enabled: StrictBool = True
    disabled_activities: tuple[StrictStr, ...] = ()
    qq_group_ids: tuple[StrictStr, ...] = ()
    qq_private_ids: tuple[StrictStr, ...] = ()
    qq_platform_ids: tuple[StrictStr, ...] = ()
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)
    chat_context: ChatContextSettings = Field(default_factory=ChatContextSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    relationships: RelationshipSettings = Field(default_factory=RelationshipSettings)

    @field_validator("agent_id")
    @classmethod
    def identity(cls, value):
        return AgentState.validate_agent_id(value)

    @field_validator("disabled_activities", "qq_group_ids", "qq_private_ids", "qq_platform_ids")
    @classmethod
    def identities(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("标识列表不能重复")
        for value in values:
            AgentState.validate_agent_id(value)
        return values

    def arbitration(self) -> ArbitrationPolicy:
        return ArbitrationPolicy(autonomous_enabled=self.autonomous_enabled,
                                 disabled_definition_ids=self.disabled_activities)
