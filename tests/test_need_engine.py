"""需求自然演化的关键行为回归，由开发者手动运行。"""

import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from pydantic import ValidationError

from application.needs import NEEDS_TICK_EVENT_TYPE, NeedTickHandler, build_need_tick, build_need_proposal
from application.persistence import PersonaStore
from core.events import AgentEvent
from core.router import EventRouter
from core.runtime import AgentRuntime
from core.scheduler import ScheduleStatus, TimeScheduler
from core.settlement import NeedTimeConflictError, StateChangeProposal, settle_state
from core.state import AgentState
from infrastructure.storage import SQLiteStorage
from persona.needs import NeedEngine, NeedRates, NeedState, NeedTimeAdvance


class NeedEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "needs.sqlite3"
        self.store = PersonaStore(SQLiteStorage(self.path))
        await self.store.open()
        self.time = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.now = self.time
        self.rates = NeedRates(energy=-4, satiety=-6, boredom=5, social_need=3)
        self.initial = AgentState(
            agent_id="persona-001", needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
            updated_at=self.time,
        )
        await self.store.create_state(self.initial)
        self.router = EventRouter()
        self.router.register(NEEDS_TICK_EVENT_TYPE, NeedTickHandler(clock=lambda: self.now))
        self.runtime = AgentRuntime("persona-001", self.store, self.router, clock=lambda: self.now)
        await self.runtime.start()
        self.scheduler = TimeScheduler("persona-001", self.store, self.runtime.submit, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.scheduler.stop()
        await self.runtime.stop()
        await self.store.close()
        self.directory.cleanup()

    async def read(self):
        return await self.store.get_state("persona-001")

    async def boot(self, rates=None):
        self.rates = self.rates if rates is None else rates
        plan = await self.store.start_needs_clock("persona-001", rates=self.rates, now=self.now)
        await self.scheduler.run_due()
        return plan

    def next_plan(self, state):
        return build_need_tick("persona-001", from_at=state.needs_updated_at, rates=self.rates)

    async def test_elapsed_time_not_tick_count_and_saturation(self):
        engine = NeedEngine(self.rates)
        whole = engine.advance(self.initial.needs, from_at=self.time, to_at=self.time + timedelta(hours=1))
        current = self.initial.needs
        for index in range(4):
            update = engine.advance(
                current, from_at=self.time + timedelta(minutes=15 * index),
                to_at=self.time + timedelta(minutes=15 * (index + 1)),
            )
            current = NeedState(**{
                name: getattr(current, name) + getattr(update.delta, name) for name in NeedState.model_fields
            })
        expected = {name: getattr(self.initial.needs, name) + getattr(whole.delta, name) for name in NeedState.model_fields}
        for name, value in expected.items():
            self.assertAlmostEqual(getattr(current, name), value)
        bounded = NeedState(energy=1, satiety=1, boredom=99, social_need=99)
        update = engine.advance(bounded, from_at=self.time, to_at=self.time + timedelta(days=365))
        self.assertEqual(update.delta.model_dump(), {"energy": -1, "satiety": -1, "boredom": 1, "social_need": 1})
        saturated = NeedState(energy=0, satiety=0, boredom=100, social_need=100)
        self.assertIsNone(engine.advance(saturated, from_at=self.time, to_at=self.time + timedelta(hours=1)).delta)
        self.assertIsNone(engine.advance(current, from_at=self.time, to_at=self.time))
        with self.assertRaises(ValueError):
            engine.advance(current, from_at=self.time, to_at=self.time - timedelta(seconds=1))
        for rates in (
            {**self.rates.model_dump(), "energy": True}, {**self.rates.model_dump(), "satiety": 1},
            {**self.rates.model_dump(), "boredom": float("inf")},
        ):
            with self.subTest(rates=rates), self.assertRaises(ValidationError):
                NeedRates(**rates)

    async def test_legacy_state_first_tick_only_establishes_baseline(self):
        with closing(sqlite3.connect(self.path)) as connection:
            legacy = self.initial.model_dump(mode="json")
            legacy.pop("needs_updated_at")
            connection.execute("UPDATE agent_states SET state_json = ?", (json.dumps(legacy),))
            connection.commit()
        self.now += timedelta(days=2)
        await self.boot()
        state = await self.read()
        self.assertEqual(state.needs, self.initial.needs)
        self.assertEqual(state.needs_updated_at, self.now)
        next_plan = self.next_plan(state)
        saved = await self.store.get_schedule("persona-001", next_plan.schedule_id)
        self.assertEqual(saved.schedule.due_at, self.now + timedelta(seconds=60))
        self.assertEqual(await self.store.start_needs_clock("persona-001", rates=self.rates, now=self.now), saved)
        with self.assertRaises(ValueError):
            await self.store.start_needs_clock(
                "persona-001", rates=NeedRates(energy=-1, satiety=-1, boredom=1, social_need=1), now=self.now,
            )

    async def test_normal_effect_keeps_time_cursor_and_replay_does_not_repeat_decay(self):
        await self.boot()
        baseline = await self.read()
        tick = self.next_plan(baseline)
        self.now += timedelta(minutes=30)
        effect = AgentEvent(
            event_id="ordinary-effect", event_type="INTERNAL.needs_update", source="test",
            timestamp=self.now, provenance="observed", payload={},
        )
        await self.store.record_event(effect)
        state = await self.store.commit_proposal(StateChangeProposal(
            proposal_id="ordinary-proposal", event_id=effect.event_id, agent_id="persona-001",
            expected_version=baseline.version, needs_delta={"energy": 10},
        ), settled_at=self.now)
        self.assertEqual(state.needs_updated_at, self.time)
        self.now = self.time + timedelta(hours=1)
        await self.scheduler.run_due()
        state = await self.read()
        self.assertEqual(state.needs.model_dump(), {"energy": 46, "satiety": 64, "boredom": 35, "social_need": 53})
        self.assertEqual(state.needs_updated_at, self.now)
        self.assertEqual(await self.runtime.submit(tick.event), state)
        self.assertEqual(await self.read(), state)

    async def test_restart_catches_up_once_and_confirmation_failure_is_safe(self):
        await self.boot()
        baseline = await self.read()
        tick = self.next_plan(baseline)
        await self.runtime.stop()
        await self.store.close()
        self.now += timedelta(hours=2)
        await self.store.open()
        await self.runtime.start()
        recovered = await self.store.start_needs_clock("persona-001", rates=self.rates, now=self.now)
        self.assertEqual(recovered.schedule, tick)
        original = self.store.record_schedule_delivery
        self.store.record_schedule_delivery = AsyncMock(side_effect=RuntimeError("confirmation failed"))
        try:
            with self.assertRaisesRegex(RuntimeError, "confirmation failed"):
                await self.scheduler.run_due()
        finally:
            self.store.record_schedule_delivery = original
        state = await self.read()
        self.assertEqual(state.needs.model_dump(), {"energy": 32, "satiety": 58, "boredom": 40, "social_need": 56})
        self.assertEqual(state.needs_updated_at, self.now)
        pending = self.next_plan(state)
        self.assertEqual(pending.due_at, self.now + timedelta(seconds=60))
        await self.scheduler.run_due()
        self.assertEqual(await self.read(), state)
        self.assertEqual((await self.store.get_schedule("persona-001", tick.schedule_id)).status, ScheduleStatus.DELIVERED)
        self.assertEqual((await self.store.get_schedule("persona-001", pending.schedule_id)).status, ScheduleStatus.PENDING)

    async def test_zero_rates_checkpoint_and_next_plan_roll_back_together(self):
        await self.boot(NeedRates(energy=0, satiety=0, boredom=0, social_need=0))
        baseline = await self.read()
        tick = self.next_plan(baseline)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""CREATE TRIGGER fail_need_plan BEFORE INSERT ON scheduled_events
                BEGIN SELECT RAISE(ABORT, 'next need plan failure'); END;""")
            connection.commit()
        self.now += timedelta(hours=1)
        self.assertEqual((await self.scheduler.run_due()).failed, (tick.schedule_id,))
        self.assertEqual(await self.read(), baseline)
        self.assertIsNone(await self.store.get_committed_proposal("persona-001", tick.event.event_id))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TRIGGER fail_need_plan")
            connection.commit()
        self.now += timedelta(seconds=5)
        await self.scheduler.run_due()
        state = await self.read()
        self.assertEqual(state.needs, baseline.needs)
        self.assertEqual(state.needs_updated_at, self.now)
        self.assertIsNone((await self.store.get_committed_proposal("persona-001", tick.event.event_id)).needs_delta)
        self.assertIsNotNone(await self.store.get_schedule("persona-001", self.next_plan(state).schedule_id))

    async def test_mismatched_window_and_altered_proposal_are_rejected(self):
        await self.boot()
        state = await self.read()
        tick = self.next_plan(state)
        self.now += timedelta(hours=1)
        proposal = build_need_proposal(tick.event, state, to_at=self.now)
        wrong_window = StateChangeProposal(**{
            **proposal.model_dump(), "needs_time": NeedTimeAdvance(
                from_at=self.time + timedelta(seconds=1), to_at=self.now,
            ),
        })
        with self.assertRaises(NeedTimeConflictError):
            settle_state(state, wrong_window, settled_at=self.now)
        await self.store.record_event(tick.event)
        changed = StateChangeProposal(**{**proposal.model_dump(), "needs_delta": {"energy": -99}})
        with self.assertRaises(ValueError):
            await self.store.commit_proposal(changed, settled_at=self.now)
        self.assertEqual(await self.read(), state)
