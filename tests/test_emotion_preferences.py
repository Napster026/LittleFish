"""情绪活动偏好、评分解释和事件接线回归，由开发者手动运行。"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from persona_agent.activity.emotion_preferences import ActivityEmotionPreference, EmotionPreferencePolicy
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.models import ActivityDefinition, ExecutionMode
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.activity.selection import ActivitySelector
from persona_agent.activity.service import ActivityService
from persona_agent.application.autonomy import AutonomousActivityService, selection_id
from persona_agent.application.emotion import (
    EMOTION_IMPULSE_EVENT_TYPE, EmotionEventHandler, build_emotion_impulse,
)
from persona_agent.application.persistence import PersonaStore
from persona_agent.core.arbiter import ArbitrationPolicy, BehaviorArbiter, BehaviorDecision
from persona_agent.core.events import AgentEvent
from persona_agent.core.router import EventRouter
from persona_agent.core.runtime import AgentRuntime
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.persona.emotion import EmotionDynamics, EmotionEngine, EmotionImpulse, EmotionState, VAD
from persona_agent.persona.needs import NeedState


TIME = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
NEEDS = NeedState(energy=20, satiety=100, boredom=0, social_need=0)


def definition(identifier, **effects):
    return ActivityDefinition(
        definition_id=identifier, name=identifier, supported_modes=["simulated"],
        estimated_duration_seconds=10, completion_needs_delta=effects or {"energy": 50},
    )


def emotion(value):
    return EmotionState(
        current=VAD(valence=value, arousal=0, dominance=0),
        mood=VAD(valence=0, arousal=0, dominance=0),
        baseline=VAD(valence=0, arousal=0, dominance=0), updated_at=TIME,
        dynamics=EmotionDynamics(emotion_half_life_seconds=1000, mood_half_life_seconds=10000,
                                 mood_impulse_ratio=0.1),
    )


def preferences():
    return tuple(ActivityEmotionPreference(
        definition_id=identifier, weights=VAD(valence=weight, arousal=0, dominance=0),
    ) for identifier, weight in (("a", -1), ("b", 1)))


def selector(*, enabled=True):
    return ActivitySelector(
        (definition("a"), definition("b")), emotion_preferences=preferences(),
        emotion_policy=EmotionPreferencePolicy(max_adjustment=0.3 if enabled else 0),
    )


class EmotionPreferenceTests(unittest.TestCase):
    def test_emotion_changes_preference_and_defaults_preserve_selection(self):
        enabled = selector()
        self.assertEqual(enabled.choose(NEEDS, emotion=emotion(-0.5)).definition.definition_id, "a")
        self.assertEqual(enabled.choose(NEEDS, emotion=emotion(0.5)).definition.definition_id, "b")
        plain = ActivitySelector(enabled.definitions).choose(NEEDS)
        for snapshot in (None, emotion(-1), emotion(1)):
            self.assertEqual(selector(enabled=False).choose(NEEDS, emotion=snapshot), plain)
        self.assertEqual(enabled.choose(NEEDS), plain)
        no_preferences = ActivitySelector(enabled.definitions, emotion_policy=EmotionPreferencePolicy(max_adjustment=0.3))
        self.assertEqual(no_preferences.choose(NEEDS, emotion=emotion(1)), plain)
        self.assertFalse(no_preferences.emotion_influence_enabled)
        self.assertFalse(selector(enabled=False).emotion_influence_enabled)

    def test_absolute_relative_caps_and_mood_blending(self):
        weights = VAD(valence=1, arousal=1, dominance=1)
        snapshot = EmotionState(**{**emotion(0).model_dump(),
                                  "current": weights, "mood": VAD(valence=-1, arousal=-1, dominance=-1)})
        relative = EmotionPreferencePolicy(max_adjustment=0.9, max_relative_adjustment=0.1)
        self.assertAlmostEqual(relative.adjustment(0.5, weights, snapshot), 0.05)
        absolute = EmotionPreferencePolicy(max_adjustment=0.01, max_relative_adjustment=1)
        self.assertAlmostEqual(absolute.adjustment(0.5, weights, snapshot), 0.01)
        balanced = EmotionPreferencePolicy(max_adjustment=1, current_weight=0.5)
        self.assertEqual(balanced.adjustment(0.5, weights, snapshot), 0)
        mood = EmotionPreferencePolicy(max_adjustment=1, current_weight=0)
        self.assertAlmostEqual(mood.adjustment(0.5, weights, snapshot), -0.25)

    def test_emotion_cannot_promote_nonbeneficial_or_nonurgent_activity(self):
        harmful = definition("harmful", energy=50, satiety=-100)
        configured = ActivitySelector((harmful,), emotion_policy=EmotionPreferencePolicy(max_adjustment=1),
                                     emotion_preferences=(ActivityEmotionPreference(
                                         definition_id="harmful", weights=VAD(valence=1, arousal=1, dominance=1),
                                     ),))
        evaluation = configured.evaluate(NEEDS, emotion=emotion(1))[0]
        self.assertLess(evaluation.score, 0)
        self.assertEqual(evaluation.emotion_adjustment, 0)
        self.assertIsNone(configured.choose(NEEDS, emotion=emotion(1)).definition)
        full = NeedState(energy=100, satiety=100, boredom=0, social_need=0)
        self.assertIsNone(selector().choose(full, emotion=emotion(1)).definition)
        self.assertTrue(all(e.emotion_adjustment == 0 for e in selector().evaluate(full, emotion=emotion(1))))

    def test_invalid_configuration_and_legacy_candidate_data(self):
        for value in (True, "0.1", float("inf"), float("nan"), -0.1, 1.1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                EmotionPreferencePolicy(max_adjustment=value)
        with self.assertRaises(ValueError):
            ActivitySelector((definition("a"),), emotion_preferences=preferences())
        with self.assertRaises(ValueError):
            ActivitySelector((definition("a"),), emotion_preferences=(preferences()[0], preferences()[0]))
        old = BehaviorDecision.model_validate({
            "winner": {"candidate_id": "idle", "kind": "idle"}, "reason": "no_candidate",
        })
        self.assertIsNone(old.winner.score_breakdown)


class EmotionPreferenceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.store = PersonaStore(SQLiteStorage(Path(self.directory.name) / "preferences.sqlite3"))
        await self.store.open()
        self.now = TIME
        self.agent = "persona-001"
        self.initial = AgentState(agent_id=self.agent, needs=NEEDS, emotion=emotion(-0.5), updated_at=TIME)
        await self.store.create_state(self.initial)
        registry = ExecutorRegistry()
        for identifier in ("a", "b"):
            registry.register(identifier, ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        self.activities = ActivityService(self.agent, self.store, ActivityManager(registry), clock=lambda: self.now)
        router = EventRouter()
        router.register(EMOTION_IMPULSE_EVENT_TYPE, EmotionEventHandler(clock=lambda: self.now))
        self.runtime = AgentRuntime(self.agent, self.store, router, clock=lambda: self.now)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.stop()
        await self.store.close()
        self.directory.cleanup()

    def service(self, configured=None, arbiter=None):
        return AutonomousActivityService(
            self.agent, self.store, configured or selector(), self.activities, self.runtime,
            clock=lambda: self.now, arbiter=arbiter,
        )

    async def impact(self):
        original = AgentEvent(event_id="source", event_type="SOCIAL.message", timestamp=self.now,
                              source="test", provenance="observed", payload={})
        await self.store.record_event(original)
        return build_emotion_impulse(self.agent, original, EmotionImpulse(valence=1, arousal=0, dominance=0))

    async def test_event_triggers_choice_receipt_and_replay(self):
        service = self.service()
        event = await self.impact()
        await service.handle_event(event)
        record = await self.store.get_activity(self.agent, selection_id(self.agent, event.event_id))
        self.assertEqual(record.instance.definition_id, "b")
        receipt = await self.store.get_event(record.instance.activity_id)
        breakdown = receipt.payload["arbitration"]["winner"]["score_breakdown"]
        self.assertAlmostEqual(breakdown["need_score"], 0.55)
        self.assertGreater(breakdown["emotion_adjustment"], 0)
        self.assertEqual(breakdown["final_score"], breakdown["need_score"] + breakdown["emotion_adjustment"])
        self.assertEqual(receipt.payload["scores"]["b"], breakdown["need_score"])
        self.assertEqual(receipt.payload["emotion_snapshot"]["current"]["valence"], 0.5)
        self.assertEqual(len(receipt.payload["emotion_preferences"]), 2)
        before = await self.store.get_state(self.agent)
        await service.handle_event(event)
        self.assertEqual(await self.store.get_state(self.agent), before)
        self.assertEqual(await self.store.get_activity(self.agent, record.instance.activity_id), record)

    async def test_default_does_not_add_event_trigger_and_disabled_candidate_stays_blocked(self):
        event = await self.impact()
        await self.service(selector(enabled=False)).handle_event(event)
        self.assertIsNone(await self.store.get_event(selection_id(self.agent, event.event_id)))
        service = self.service(arbiter=BehaviorArbiter(ArbitrationPolicy(disabled_definition_ids={"b"})))
        started = await service.consider("disabled-b")
        self.assertEqual(started.instance.definition_id, "a")
        denied = next(a for a in service.last_decision.assessments if a.candidate.definition_id == "b")
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "activity_disabled")

    async def test_projection_does_not_commit_emotion_and_pending_is_not_reranked(self):
        self.now += timedelta(seconds=1000)
        configured = selector()
        reserved = await self.store.reserve_behavior(
            self.agent, configured, decision_key="original", now=self.now, arbiter=BehaviorArbiter(),
        )
        projected = EmotionEngine().advance(self.initial.emotion, to_at=self.now).state
        expected = next(e for e in configured.evaluate(NEEDS, emotion=projected) if e.definition.definition_id == "a")
        self.assertEqual(reserved.decision.winner.score_breakdown.emotion_adjustment, expected.emotion_adjustment)
        self.assertEqual(await self.store.get_state(self.agent), self.initial)
        await self.runtime.submit(await self.impact())
        # 情绪反转后只接续原 a，不用新情绪把已保存的 pending 替换成 b。
        await self.store.close()
        await self.store.open()
        recovered = await self.service().consider("original")
        self.assertEqual(recovered.instance.activity_id, reserved.activity.instance.activity_id)
        self.assertEqual(recovered.instance.definition_id, "a")
