"""情绪时间演化、事件冲击和事务集成，由开发者手动运行。"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from persona_agent.application.emotion import (
    EMOTION_IMPULSE_EVENT_TYPE, EMOTION_TICK_EVENT_TYPE, EmotionEventHandler,
    build_emotion_initialization, build_emotion_impulse, build_emotion_proposal,
)
from persona_agent.application.needs import NEEDS_TICK_EVENT_TYPE, NeedTickHandler
from persona_agent.application.persistence import PersonaStore
from persona_agent.core.events import AgentEvent
from persona_agent.core.persistence import CorePersistence
from persona_agent.core.router import EventRouter
from persona_agent.core.runtime import AgentRuntime
from persona_agent.core.scheduler import TimeScheduler
from persona_agent.core.settlement import EmotionTimeConflictError, StateChangeProposal, settle_state
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.persona.emotion import EmotionChange, EmotionDynamics, EmotionEngine, EmotionImpulse, EmotionState, VAD
from persona_agent.persona.needs import NeedRates, NeedState


TIME = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def emotion(**changes):
    data = dict(
        current=VAD(valence=0.8, arousal=0, dominance=0),
        mood=VAD(valence=0, arousal=0, dominance=0),
        baseline=VAD(valence=0, arousal=0, dominance=0), updated_at=TIME,
        dynamics=EmotionDynamics(
            emotion_half_life_seconds=10, mood_half_life_seconds=100,
            mood_impulse_ratio=0.1, tick_interval_seconds=10,
        ),
    )
    data.update(changes)
    return EmotionState(**data)


class EmotionEngineTests(unittest.TestCase):
    def test_half_life_coupled_regression_and_partition_invariance(self):
        engine = EmotionEngine()
        initial = emotion()
        self.assertAlmostEqual(engine.advance(initial, to_at=TIME + timedelta(seconds=10)).state.current.valence, 0.4)
        initial = emotion(mood=VAD(valence=0.4, arousal=-0.3, dominance=0.2))
        direct = engine.advance(initial, to_at=TIME + timedelta(seconds=37)).state
        middle = engine.advance(initial, to_at=TIME + timedelta(seconds=13)).state
        split = engine.advance(middle, to_at=TIME + timedelta(seconds=37)).state
        for layer in ("current", "mood"):
            for name in ("valence", "arousal", "dominance"):
                self.assertAlmostEqual(getattr(getattr(direct, layer), name), getattr(getattr(split, layer), name), places=12)
        distant = engine.advance(initial, to_at=TIME + timedelta(days=100)).state
        self.assertEqual(distant.current, initial.baseline)
        self.assertEqual(distant.mood, initial.baseline)
        self.assertEqual(initial.updated_at, TIME)

    def test_impulse_applies_after_regression_and_clamps(self):
        initial = emotion()
        changed = EmotionEngine().apply_impulse(
            initial, EmotionImpulse(valence=0.2, arousal=0, dominance=0), at=TIME + timedelta(seconds=10),
        )
        self.assertAlmostEqual(changed.state.current.valence, 0.6)
        self.assertAlmostEqual(changed.state.mood.valence, 0.02)
        self.assertEqual(changed.state.baseline, initial.baseline)
        self.assertEqual(changed.state.dynamics, initial.dynamics)
        saturated = EmotionEngine().apply_impulse(
            initial, EmotionImpulse(valence=1, arousal=-1, dominance=1), at=TIME,
        )
        self.assertEqual(saturated.state.current.valence, 1)
        self.assertEqual(saturated.state.current.arousal, -1)
        self.assertEqual(initial.current.valence, 0.8)

    def test_invalid_values_and_time(self):
        for value in (True, "0", float("nan"), float("inf"), 1.1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                VAD(valence=value, arousal=0, dominance=0)
        with self.assertRaises(ValidationError):
            EmotionImpulse(valence=0, arousal=0, dominance=0)
        with self.assertRaises(ValidationError):
            EmotionDynamics(emotion_half_life_seconds=100, mood_half_life_seconds=10, mood_impulse_ratio=0.1)
        for at in (TIME - timedelta(seconds=1), TIME.replace(tzinfo=None), None):
            with self.subTest(at=at), self.assertRaises(ValueError):
                EmotionEngine().advance(emotion(), to_at=at)
        self.assertIsNone(EmotionEngine().advance(emotion(), to_at=TIME))


class EmotionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.store = PersonaStore(SQLiteStorage(Path(self.directory.name) / "emotion.sqlite3"))
        await self.store.open()
        self.now = TIME
        self.agent = "persona-001"
        self.initial = AgentState(
            agent_id=self.agent, updated_at=TIME,
            needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
        )
        await self.store.create_state(self.initial)
        router = EventRouter()
        handler = EmotionEventHandler(clock=lambda: self.now)
        router.register(EMOTION_TICK_EVENT_TYPE, handler)
        router.register(EMOTION_IMPULSE_EVENT_TYPE, handler)
        router.register(NEEDS_TICK_EVENT_TYPE, NeedTickHandler(clock=lambda: self.now))
        self.runtime = AgentRuntime(self.agent, self.store, router, clock=lambda: self.now)
        await self.runtime.start()
        self.scheduler = TimeScheduler(self.agent, self.store, self.runtime.submit, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.scheduler.stop()
        await self.runtime.stop()
        await self.store.close()
        self.directory.cleanup()

    async def read(self):
        return await self.store.get_state(self.agent)

    async def boot(self):
        return await self.store.start_emotion_clock(self.agent, initial=emotion(), now=self.now)

    async def impact(self, identifier="source", *, save_source=True):
        source = AgentEvent(
            event_id=identifier, event_type="SOCIAL.message", timestamp=self.now,
            source="test", provenance="observed", payload={},
        )
        if save_source:
            await self.store.record_event(source)
        return build_emotion_impulse(self.agent, source, EmotionImpulse(valence=0.2, arousal=0, dominance=0))

    async def test_explicit_initialization_and_tick_recovery(self):
        self.assertIsNone((await self.read()).emotion)
        with self.assertRaises(ValueError):
            await self.store.start_emotion_clock(self.agent, now=self.now)
        plan = await self.boot()
        self.assertEqual((await self.read()).emotion, emotion())
        self.now += timedelta(seconds=35)
        await self.store.close()
        await self.store.open()
        self.assertEqual(await self.store.start_emotion_clock(self.agent, now=self.now), plan)
        await self.scheduler.run_due()
        state = await self.read()
        expected = EmotionEngine().advance(emotion(), to_at=self.now).state
        self.assertEqual(state.emotion, expected)
        self.assertEqual(state.version, 2)
        self.assertEqual(state.needs, self.initial.needs)
        await self.runtime.submit(plan.schedule.event)
        self.assertEqual(await self.read(), state)

    async def test_impact_idempotency_stale_tick_and_latest_state_replay(self):
        old_plan = await self.boot()
        self.now += timedelta(seconds=10)
        event = await self.impact()
        result = await self.runtime.submit(event)
        self.assertAlmostEqual(result.emotion.current.valence, 0.6)
        self.assertAlmostEqual(result.emotion.mood.valence, 0.02)
        self.assertEqual(await self.runtime.submit(event), result)
        # 冲击已推进同一时间段，旧 tick 不再重复回归。
        await self.scheduler.run_due()
        self.assertEqual(await self.read(), result)
        self.assertIsNone(await self.store.get_committed_proposal(self.agent, old_plan.schedule.event.event_id))
        self.now += timedelta(seconds=10)
        await self.scheduler.run_due()
        latest = await self.read()
        await self.store.close()
        await self.store.open()
        self.assertEqual(await self.runtime.submit(event), latest)

    async def test_ordinary_need_changes_preserve_emotion_and_its_cursor(self):
        await self.boot()
        await self.store.start_needs_clock(
            self.agent, rates=NeedRates(energy=-4, satiety=-6, boredom=5, social_need=3), now=self.now,
        )
        await self.scheduler.run_due()
        before = await self.read()
        self.now += timedelta(seconds=5)
        event = AgentEvent(event_id="needs-effect", event_type="INTERNAL.need_effect", timestamp=self.now,
                           source="test", provenance="observed", payload={})
        await self.store.record_event(event)
        changed = await self.store.commit_proposal(StateChangeProposal(
            proposal_id="needs-effect-proposal", event_id=event.event_id, agent_id=self.agent,
            expected_version=before.version, needs_delta={"energy": 10},
        ), settled_at=self.now)
        self.assertEqual(changed.emotion, before.emotion)
        self.assertEqual(changed.needs_updated_at, before.needs_updated_at)
        self.assertEqual(changed.needs.energy, 50)

    async def test_transaction_rollback_and_retry(self):
        with patch.object(CorePersistence, "create_schedule", AsyncMock(side_effect=RuntimeError("write failed"))):
            with self.assertRaises(RuntimeError):
                await self.boot()
        self.assertEqual(await self.read(), self.initial)
        init_event = build_emotion_initialization(self.agent, emotion())
        self.assertIsNone(await self.store.get_event(init_event.event_id))
        await self.boot()
        self.now += timedelta(seconds=5)
        event = await self.impact()
        before = await self.read()
        with patch.object(CorePersistence, "create_schedule", AsyncMock(side_effect=RuntimeError("write failed"))):
            with self.assertRaises(RuntimeError):
                await self.runtime.submit(event)
        self.assertEqual(await self.read(), before)
        self.assertIsNone(await self.store.get_committed_proposal(self.agent, event.event_id))
        self.assertIsNotNone(await self.store.get_event(event.event_id))
        retried = await self.runtime.submit(event)
        expected = EmotionEngine().apply_impulse(
            before.emotion, EmotionImpulse.model_validate(event.payload["impulse"]), at=self.now,
        )
        self.assertEqual(retried.emotion, expected.state)
        self.assertEqual(retried.version, before.version + 1)

    async def test_missing_source_tampered_proposal_and_time_conflicts(self):
        plan = await self.boot()
        self.now += timedelta(seconds=10)
        with self.assertRaises(ValueError):
            await self.runtime.submit(await self.impact(save_source=False))
        state = await self.read()
        await self.store.record_event(plan.schedule.event)
        proposal = build_emotion_proposal(plan.schedule.event, state, to_at=self.now)
        changed_state = EmotionState(**{**proposal.emotion_change.state.model_dump(), "current": {
            "valence": 0.99, "arousal": 0, "dominance": 0,
        }})
        tampered = StateChangeProposal(**{**proposal.model_dump(), "emotion_change": EmotionChange(
            from_at=TIME, state=changed_state,
        )})
        with self.assertRaises(ValueError):
            await self.store.commit_proposal(tampered, settled_at=self.now)
        wrong_time = StateChangeProposal(**{**proposal.model_dump(), "emotion_change": EmotionChange(
            from_at=TIME + timedelta(seconds=1), state=proposal.emotion_change.state,
        )})
        with self.assertRaises(EmotionTimeConflictError):
            settle_state(state, wrong_time, settled_at=self.now)
        changed_baseline = EmotionState(**{**proposal.emotion_change.state.model_dump(), "baseline": {
            "valence": 0.1, "arousal": 0, "dominance": 0,
        }})
        wrong_rules = StateChangeProposal(**{**proposal.model_dump(), "emotion_change": EmotionChange(
            from_at=TIME, state=changed_baseline,
        )})
        with self.assertRaises(EmotionTimeConflictError):
            settle_state(state, wrong_rules, settled_at=self.now)
        self.assertEqual(await self.read(), state)
