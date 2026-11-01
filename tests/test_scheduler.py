"""持久化定时事件、重试、运行时投递与结构迁移测试，由开发者手动运行。"""

from schema_fixtures import restore_v3_layout

import asyncio
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from pydantic import ValidationError

from persona_agent.activity.executor import ActivityContext
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.models import ActivityDefinition, ExecutionMode, create_activity_instance
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.core.events import AgentEvent
from persona_agent.core.router import EventRouter
from persona_agent.core.runtime import AgentRuntime
from persona_agent.core.scheduler import ScheduledEvent, ScheduleStatus, SchedulerError, TimeScheduler
from persona_agent.core.settlement import StateChangeProposal
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.application.persistence import PersonaStore
from persona_agent.persistence.errors import (
    ScheduleConflictError,
    ScheduleVersionConflictError,
    StateNotFoundError,
)
from persona_agent.persona.needs import NeedState


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "agent.sqlite3"
        self.storage = PersonaStore(SQLiteStorage(self.path))
        await self.storage.open()
        self.time = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.now = self.time
        self.state = AgentState(
            agent_id="persona-001", updated_at=self.time,
            needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
        )
        await self.storage.create_state(self.state)
        self.delivered = []
        self.schedulers = []

    async def asyncTearDown(self):
        for scheduler in self.schedulers:
            await scheduler.stop()
        await self.storage.close()
        self.directory.cleanup()

    def event(self, schedule_id="job-001", due_seconds=0, **updates):
        due = self.time + timedelta(seconds=due_seconds)
        event = AgentEvent(
            event_id=f"event:{schedule_id}", event_type="INTERNAL.needs_update", timestamp=due,
            source="scheduler", provenance="observed", payload={"energy": 1},
        )
        return ScheduledEvent(**{
            "schedule_id": schedule_id, "agent_id": self.state.agent_id,
            "event": event, "due_at": due, **updates,
        })

    def make_scheduler(self, deliver=None, **updates):
        async def default_deliver(event):
            self.delivered.append(event)

        options = dict(poll_interval=3600, clock=lambda: self.now)
        options.update(updates)
        scheduler = TimeScheduler(
            self.state.agent_id, self.storage,
            default_deliver if deliver is None else deliver, **options,
        )
        self.schedulers.append(scheduler)
        return scheduler

    async def read(self, schedule_id="job-001"):
        return await self.storage.get_schedule(self.state.agent_id, schedule_id)

    async def test_schedule_json_round_trip_and_validation(self):
        event = self.event()
        self.assertEqual(ScheduledEvent.model_validate_json(event.model_dump_json()), event)
        for field, values in {
            "schedule_id": ["", " id", 123],
            "agent_id": ["", None],
            "due_at": [datetime(2026, 10, 31), 123, "123"],
            "max_attempts": [0, True, "3", 101],
            "retry_delay_seconds": [0, True, "5", float("inf"), 86401],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        ScheduledEvent(**{**event.model_dump(), field: value})

    async def test_schedule_survives_reopen_and_is_role_scoped(self):
        original = await self.storage.create_schedule(self.event())
        await self.storage.close()
        await self.storage.open()
        self.assertEqual(await self.read(), original)
        self.assertIsNone(await self.storage.get_schedule("other", "job-001"))

    async def test_same_id_retries_do_not_reset_delivery(self):
        plan = self.event()
        await self.storage.create_schedule(plan)
        saved = await self.storage.record_schedule_delivery(
            self.state.agent_id, plan.schedule_id, expected_attempts=0,
            attempted_at=self.now, succeeded=True,
        )
        self.assertEqual(await self.storage.create_schedule(plan), saved)
        with self.assertRaises(ScheduleConflictError):
            await self.storage.create_schedule(self.event(due_seconds=1))

    async def test_due_selection_orders_and_normalizes_time(self):
        for schedule_id, seconds in (("b", 2), ("c", 1), ("a", 1)):
            await self.storage.create_schedule(self.event(schedule_id, seconds))
        now = (self.time + timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8)))
        records = await self.storage.list_due_schedules(self.state.agent_id, now=now)
        self.assertEqual([record.schedule.schedule_id for record in records], ["a", "c"])
        limited = await self.storage.list_due_schedules(self.state.agent_id, now=now, limit=1)
        self.assertEqual(limited[0].schedule.schedule_id, "a")

    async def test_successful_schedule_is_not_delivered_again(self):
        scheduler = self.make_scheduler()
        await scheduler.schedule(self.event())
        self.assertEqual((await scheduler.run_due()).delivered, ("job-001",))
        self.assertEqual((await scheduler.run_due()).delivered, ())
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual((await self.read()).status, ScheduleStatus.DELIVERED)
        self.assertEqual((await self.read()).attempts, 1)

    async def test_failure_waits_then_retries_original_event(self):
        calls = []

        async def deliver(event):
            calls.append(event)
            if len(calls) == 1:
                raise RuntimeError("temporary failure")

        scheduler = self.make_scheduler(deliver)
        plan = self.event()
        await scheduler.schedule(plan)
        self.assertEqual((await scheduler.run_due()).failed, ("job-001",))
        record = await self.read()
        self.assertEqual(record.attempts, 1)
        self.assertEqual(record.next_attempt_at, self.time + timedelta(seconds=5))
        self.now += timedelta(seconds=4)
        self.assertEqual((await scheduler.run_due()).delivered, ())
        self.now += timedelta(seconds=1)
        self.assertEqual((await scheduler.run_due()).delivered, ("job-001",))
        self.assertEqual(calls, [plan.event, plan.event])
        self.assertIsNone((await self.read()).last_error)
        self.assertEqual((await self.read()).attempts, 2)

    async def test_exhausted_attempts_enter_failed_without_automatic_reset(self):
        deliver = AsyncMock(side_effect=RuntimeError("permanent failure"))
        scheduler = self.make_scheduler(deliver)
        plan = self.event(max_attempts=2)
        await scheduler.schedule(plan)
        await scheduler.run_due()
        self.now += timedelta(seconds=5)
        await scheduler.run_due()
        self.now += timedelta(seconds=100)
        self.assertEqual((await scheduler.run_due()).failed, ())
        record = await self.read()
        self.assertEqual(record.status, ScheduleStatus.FAILED)
        self.assertEqual(record.attempts, 2)
        self.assertEqual(await scheduler.schedule(plan), record)
        self.assertEqual(deliver.await_count, 2)

    async def test_new_scheduler_recovers_overdue_pending_event(self):
        plan = self.event(due_seconds=10)
        await self.storage.create_schedule(plan)
        await self.storage.close()
        await self.storage.open()
        self.now += timedelta(seconds=20)
        scheduler = self.make_scheduler()
        self.assertEqual((await scheduler.run_due()).delivered, ("job-001",))
        self.assertEqual(self.delivered[0], plan.event)

    async def test_runtime_deduplicates_redelivery_after_confirmation_failure(self):
        calls = []

        async def rule(event, state):
            calls.append(event.event_id)
            return StateChangeProposal(
                proposal_id=f"proposal:{event.event_id}", event_id=event.event_id,
                agent_id=state.agent_id, expected_version=state.version, needs_delta={"energy": 1},
            )

        router = EventRouter()
        router.register("INTERNAL.needs_update", rule)
        async with AgentRuntime(self.state.agent_id, self.storage, router, clock=lambda: self.now) as runtime:
            scheduler = self.make_scheduler(runtime.submit)
            await scheduler.schedule(self.event())
            original = self.storage.record_schedule_delivery
            self.storage.record_schedule_delivery = AsyncMock(side_effect=RuntimeError("confirmation failure"))
            try:
                with self.assertRaisesRegex(RuntimeError, "confirmation failure"):
                    await scheduler.run_due()
            finally:
                self.storage.record_schedule_delivery = original
            self.assertEqual((await self.read()).status, ScheduleStatus.PENDING)
            await scheduler.run_due()
        role = await self.storage.get_state(self.state.agent_id)
        self.assertEqual(role.needs.energy, 41)
        self.assertEqual(role.version, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual((await self.read()).status, ScheduleStatus.DELIVERED)

    async def test_stale_delivery_result_cannot_overwrite_newer_attempt(self):
        await self.storage.create_schedule(self.event())
        await self.storage.record_schedule_delivery(
            self.state.agent_id, "job-001", expected_attempts=0, attempted_at=self.time,
            succeeded=False, error="failure",
        )
        later = self.time + timedelta(seconds=5)
        with self.assertRaises(ScheduleVersionConflictError):
            await self.storage.record_schedule_delivery(
                self.state.agent_id, "job-001", expected_attempts=0, attempted_at=later, succeeded=True,
            )
        result = await self.storage.record_schedule_delivery(
            self.state.agent_id, "job-001", expected_attempts=1, attempted_at=later, succeeded=True,
        )
        self.assertEqual(await self.storage.record_schedule_delivery(
            self.state.agent_id, "job-001", expected_attempts=1, attempted_at=later, succeeded=True,
        ), result)

    async def test_background_lifecycle_wakes_for_new_schedule(self):
        delivered = asyncio.Event()

        async def deliver(event):
            self.delivered.append(event)
            delivered.set()

        scheduler = self.make_scheduler(deliver)
        async with scheduler:
            self.assertTrue(scheduler.is_running)
            await scheduler.schedule(self.event())
            await asyncio.wait_for(delivered.wait(), timeout=5)
        self.assertFalse(scheduler.is_running)
        self.assertEqual((await self.read()).status, ScheduleStatus.DELIVERED)
        self.assertIsNotNone(await self.storage.get_state(self.state.agent_id))

    async def test_concurrent_run_due_calls_are_serialized(self):
        async def deliver(event):
            await asyncio.sleep(0)
            self.delivered.append(event)

        scheduler = self.make_scheduler(deliver)
        await scheduler.schedule(self.event())
        reports = await asyncio.gather(scheduler.run_due(), scheduler.run_due())
        self.assertEqual(sum(len(report.delivered) for report in reports), 1)
        self.assertEqual(len(self.delivered), 1)

    async def test_wrong_role_schedule_and_missing_role_start_are_rejected(self):
        scheduler = self.make_scheduler()
        other = self.event(agent_id="other")
        with self.assertRaises(SchedulerError):
            await scheduler.schedule(other)
        with self.assertRaises(StateNotFoundError):
            await self.storage.create_schedule(other)
        missing = TimeScheduler("missing", self.storage, AsyncMock())
        with self.assertRaises(SchedulerError):
            await missing.start()

    async def test_failed_delivery_does_not_block_other_due_events(self):
        async def deliver(event):
            if event.event_id.endswith("a-bad"):
                raise RuntimeError("bad event")
            self.delivered.append(event)

        scheduler = self.make_scheduler(deliver)
        await scheduler.schedule(self.event("a-bad"))
        await scheduler.schedule(self.event("b-good"))
        report = await scheduler.run_due()
        self.assertEqual(report.failed, ("a-bad",))
        self.assertEqual(report.delivered, ("b-good",))

    async def test_invalid_configuration_and_clock_are_rejected(self):
        for interval in (0, -1, True, "1", float("inf"), float("nan")):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    self.make_scheduler(poll_interval=interval)
        with self.assertRaises(ValueError):
            TimeScheduler("", self.storage, AsyncMock())
        with self.assertRaises(ValueError):
            TimeScheduler(self.state.agent_id, self.storage, None)
        bad_clock = self.make_scheduler(clock=lambda: datetime(2026, 10, 31))
        with self.assertRaises(ValueError):
            await bad_clock.run_due()

    async def test_start_scans_persisted_due_events_and_is_repeatable(self):
        await self.storage.create_schedule(self.event())
        delivered = asyncio.Event()

        async def deliver(event):
            self.delivered.append(event)
            delivered.set()

        scheduler = self.make_scheduler(deliver)
        await scheduler.start()
        await scheduler.start()
        await asyncio.wait_for(delivered.wait(), timeout=5)
        await scheduler.stop()
        await scheduler.stop()
        await scheduler.start()
        await scheduler.stop()
        self.assertEqual(len(self.delivered), 1)

    async def test_stop_finishes_current_delivery_but_leaves_remaining_jobs_pending(self):
        entered, release, second = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def deliver(event):
            entered.set()
            await release.wait()
            if event.event_id.endswith("B-second"):
                second.set()

        scheduler = self.make_scheduler(deliver)
        await scheduler.schedule(self.event("A-first"))
        await scheduler.schedule(self.event("B-second"))
        await scheduler.start()
        await asyncio.wait_for(entered.wait(), timeout=5)
        stopping = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0)
        try:
            self.assertFalse(scheduler.is_running)
        finally:
            release.set()
            await stopping
        self.assertEqual((await self.read("A-first")).status, ScheduleStatus.DELIVERED)
        self.assertEqual((await self.read("B-second")).status, ScheduleStatus.PENDING)
        await scheduler.start()
        await asyncio.wait_for(second.wait(), timeout=5)
        await scheduler.stop()
        self.assertEqual((await self.read("B-second")).status, ScheduleStatus.DELIVERED)

    async def test_v2_upgrade_preserves_activity_and_role(self):
        definition = ActivityDefinition(
            definition_id="rest", name="休息", supported_modes=["simulated"], estimated_duration_seconds=10,
        )
        instance = create_activity_instance(
            definition, activity_id="activity-001", agent_id=self.state.agent_id,
            mode=ExecutionMode.SIMULATED, created_at=self.time,
        )
        await self.storage.create_activity(definition, instance)
        registry = ExecutorRegistry()
        registry.register("rest", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        context = ActivityContext(definition=definition, instance=instance, now=self.time)
        result = await ActivityManager(registry).start(context)
        saved = await self.storage.commit_activity_result("start", context, result)
        await self.storage.close()
        with closing(sqlite3.connect(self.path)) as connection:
            restore_v3_layout(connection)
            connection.execute("DROP TABLE scheduled_events")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        await self.storage.open()
        self.assertEqual(await self.storage.get_activity(self.state.agent_id, instance.activity_id), saved)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)
        self.assertEqual(await self.storage.get_event(result.events[0].event_id), result.events[0])
        await self.storage.create_schedule(self.event())
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)

    async def test_failed_v2_upgrade_rolls_back_new_table_and_version(self):
        await self.storage.close()
        with closing(sqlite3.connect(self.path)) as connection:
            restore_v3_layout(connection)
            connection.execute("DROP TABLE scheduled_events")
            connection.execute("PRAGMA user_version = 2")
            connection.execute("CREATE INDEX scheduled_due_idx ON agent_states (agent_id)")
            connection.commit()
        with self.assertRaises(sqlite3.OperationalError):
            await self.storage.open()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'scheduled_events'"
            ).fetchone())
            connection.execute("DROP INDEX scheduled_due_idx")
            connection.commit()
        await self.storage.open()
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_delivery_callback_cannot_wait_for_its_own_scheduler(self):
        async def deliver(event):
            with self.assertRaises(SchedulerError):
                await scheduler.run_due()
            with self.assertRaises(SchedulerError):
                await scheduler.stop()

        scheduler = self.make_scheduler(deliver)
        await scheduler.schedule(self.event())
        self.assertEqual((await scheduler.run_due()).delivered, ("job-001",))
