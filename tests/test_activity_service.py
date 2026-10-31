"""活动服务的模拟推进、原子调度和恢复测试，由开发者手动运行。"""

import asyncio
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from pydantic import ValidationError

from persona_agent.activity.executor import ActivityContext, ActivityExecutionResult, InvalidActivityStatusError
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.models import ActivityDefinition, ActivityRecord, ActivityStatus, ExecutionMode, create_activity_instance
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.activity.service import ActivityService, ActivityServiceError
from persona_agent.activity.ticks import ActivityTick, build_activity_tick, build_tick_exhaustion_result, parse_activity_tick
from persona_agent.core.events import AgentEvent
from persona_agent.core.router import EventRouter
from persona_agent.core.runtime import AgentRuntime
from persona_agent.core.scheduler import ScheduledEvent, ScheduleStatus, TimeScheduler
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.application.persistence import PersonaStore
from persona_agent.persistence.errors import (
    ActivityNotFoundError,
    ScheduleConflictError,
)
from persona_agent.persona.needs import NeedState


class ActivityServiceTests(unittest.IsolatedAsyncioTestCase):
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
        self.definition = ActivityDefinition(
            definition_id="rest", name="休息", supported_modes=["simulated"],
            estimated_duration_seconds=10, completion_needs_delta={"energy": 10},
        )
        self.initial = create_activity_instance(
            self.definition, activity_id="activity-001", agent_id=self.state.agent_id,
            mode=ExecutionMode.SIMULATED, created_at=self.time,
        )
        await self.storage.create_activity(self.definition, self.initial)
        registry = ExecutorRegistry()
        registry.register("rest", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        self.manager = ActivityManager(registry)
        self.service = self.make_service()
        self.scheduler = TimeScheduler(
            self.state.agent_id, self.storage, self.service.handle_event,
            clock=lambda: self.now, poll_interval=0.001,
        )

    async def asyncTearDown(self):
        await self.scheduler.stop()
        await self.storage.close()
        self.directory.cleanup()

    def make_service(self, **options):
        return ActivityService(self.state.agent_id, self.storage, self.manager, clock=lambda: self.now, **options)

    async def read(self):
        return await self.storage.get_activity(self.state.agent_id, self.initial.activity_id)

    async def plan(self, record=None, interval=5):
        record = await self.read() if record is None else record
        plan = build_activity_tick(record, interval_seconds=interval)
        return await self.storage.get_schedule(self.state.agent_id, plan.schedule_id)

    def execute_sql(self, statement):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(statement)
            connection.commit()

    async def test_tick_payload_and_plan_validation(self):
        started = await self.service.start_activity(self.initial.activity_id)
        plan = build_activity_tick(started)
        self.assertEqual(ScheduledEvent.model_validate_json(plan.model_dump_json()), plan)
        self.assertEqual(plan, build_activity_tick(started))
        self.assertEqual(parse_activity_tick(plan.event).expected_revision, 1)
        self.assertEqual(plan.event.provenance.value, "observed")
        for field, values in {
            "agent_id": ["", " id", True], "expected_revision": [0, True, "1"],
            "interval_seconds": [0, 0.0001, True, "5", float("inf"), 86401],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        ActivityTick(**{**plan.event.payload, field: value})
        with self.assertRaises(ValueError):
            build_activity_tick(ActivityRecord(definition=self.definition, instance=self.initial))

    async def test_start_atomically_saves_first_tick(self):
        saved = await self.service.start_activity(self.initial.activity_id)
        self.assertEqual(saved.instance.status, ActivityStatus.RUNNING)
        self.assertEqual(saved.instance.revision, 1)
        plan = await self.plan(saved)
        self.assertEqual(plan.status, ScheduleStatus.PENDING)
        self.assertEqual(plan.schedule.due_at, self.time + timedelta(seconds=5))
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).version, 0)

    async def test_due_tick_advances_then_completion_settles_once(self):
        started = await self.service.start_activity(self.initial.activity_id)
        first = await self.plan(started)
        self.now += timedelta(seconds=5)
        self.assertEqual((await self.scheduler.run_due()).delivered, (first.schedule.schedule_id,))
        halfway = await self.read()
        self.assertAlmostEqual(halfway.instance.progress, 0.5)
        self.assertEqual(halfway.instance.revision, 2)
        self.assertEqual(await self.storage.get_event(first.schedule.event.event_id), first.schedule.event)
        second = await self.plan(halfway)
        self.now += timedelta(seconds=5)
        await self.scheduler.run_due()
        completed = await self.read()
        self.assertEqual(completed.instance.status, ActivityStatus.COMPLETED)
        state = await self.storage.get_state(self.state.agent_id)
        self.assertEqual((state.needs.energy, state.version), (50, 1))
        self.assertEqual(await self.storage.list_due_schedules(self.state.agent_id, now=self.now), ())
        await self.service.handle_event(second.schedule.event)
        await self.scheduler.run_due()
        self.assertEqual(await self.storage.get_state(self.state.agent_id), state)
        self.assertEqual(await self.read(), completed)

    async def test_remaining_duration_shortens_last_interval(self):
        service = self.make_service(tick_interval_seconds=6)
        await service.start_activity(self.initial.activity_id)
        self.now += timedelta(seconds=6)
        await self.scheduler.run_due()
        record = await self.read()
        plan = await self.plan(record, interval=6)
        self.assertEqual(plan.schedule.due_at, self.time + timedelta(seconds=10))
        self.now += timedelta(seconds=4)
        await self.scheduler.run_due()
        self.assertEqual((await self.read()).instance.status, ActivityStatus.COMPLETED)

    async def test_pausing_makes_old_tick_harmless(self):
        started = await self.service.start_activity(self.initial.activity_id)
        old_plan = await self.plan(started)
        self.now += timedelta(seconds=2)
        paused = await self.service.pause_activity(self.initial.activity_id)
        self.assertEqual(paused.instance.status, ActivityStatus.PAUSED)
        self.assertAlmostEqual(paused.instance.progress, 0.2)
        self.now += timedelta(seconds=3)
        await self.scheduler.run_due()
        self.assertEqual(await self.read(), paused)
        self.assertEqual((await self.storage.get_schedule(
            self.state.agent_id, old_plan.schedule.schedule_id,
        )).status, ScheduleStatus.DELIVERED)

    async def test_resume_excludes_pause_time_and_preserves_new_plan(self):
        await self.service.start_activity(self.initial.activity_id)
        self.now += timedelta(seconds=2)
        await self.service.pause_activity(self.initial.activity_id)
        self.now += timedelta(seconds=100)
        resumed = await self.service.resume_activity(self.initial.activity_id)
        self.assertAlmostEqual(resumed.instance.progress, 0.2)
        await self.scheduler.run_due()  # 清理旧修订的到期计划，不推进恢复后的实例。
        self.assertEqual(await self.read(), resumed)
        self.now += timedelta(seconds=5)
        await self.scheduler.run_due()
        self.assertAlmostEqual((await self.read()).instance.progress, 0.7)
        self.now += timedelta(seconds=3)
        await self.scheduler.run_due()
        self.assertEqual((await self.read()).instance.status, ActivityStatus.COMPLETED)

    async def test_stop_cancels_and_stale_tick_does_not_reward(self):
        await self.service.start_activity(self.initial.activity_id)
        self.now += timedelta(seconds=2)
        stopped = await self.service.stop_activity(self.initial.activity_id)
        self.assertEqual(stopped.instance.status, ActivityStatus.CANCELLED)
        self.now += timedelta(seconds=20)
        await self.scheduler.run_due()
        self.assertEqual(await self.read(), stopped)
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).needs.energy, 40)

    async def test_stop_after_elapsed_duration_completes_instead_of_canceling(self):
        await self.service.start_activity(self.initial.activity_id)
        self.now += timedelta(seconds=10)
        completed = await self.service.stop_activity(self.initial.activity_id)
        self.assertEqual(completed.instance.status, ActivityStatus.COMPLETED)
        await self.scheduler.run_due()
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).version, 1)

    async def test_overdue_tick_after_reopen_catches_up(self):
        await self.service.start_activity(self.initial.activity_id)
        await self.storage.close()
        await self.storage.open()
        self.now += timedelta(seconds=100)
        service = self.make_service(tick_interval_seconds=20)
        scheduler = TimeScheduler(self.state.agent_id, self.storage, service.handle_event, clock=lambda: self.now)
        await scheduler.run_due()
        self.assertEqual((await self.read()).instance.status, ActivityStatus.COMPLETED)
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).needs.energy, 50)

    async def test_recreated_service_uses_interval_from_persisted_tick(self):
        await self.make_service(tick_interval_seconds=3).start_activity(self.initial.activity_id)
        self.now += timedelta(seconds=3)
        service = self.make_service(tick_interval_seconds=20)
        scheduler = TimeScheduler(self.state.agent_id, self.storage, service.handle_event, clock=lambda: self.now)
        await scheduler.run_due()
        self.assertEqual((await self.plan(interval=3)).schedule.due_at, self.time + timedelta(seconds=6))

    async def test_confirmation_failure_redelivery_does_not_repeat_step(self):
        started = await self.service.start_activity(self.initial.activity_id)
        old_plan = await self.plan(started)
        self.now += timedelta(seconds=5)
        original = self.storage.record_schedule_delivery
        self.storage.record_schedule_delivery = AsyncMock(side_effect=RuntimeError("confirmation failure"))
        try:
            with self.assertRaisesRegex(RuntimeError, "confirmation failure"):
                await self.scheduler.run_due()
        finally:
            self.storage.record_schedule_delivery = original
        advanced = await self.read()
        self.assertEqual(advanced.instance.revision, 2)
        next_plan = await self.plan(advanced)
        await self.scheduler.run_due()
        self.assertEqual(await self.read(), advanced)
        self.assertEqual(await self.plan(advanced), next_plan)
        self.assertEqual((await self.storage.get_schedule(
            self.state.agent_id, old_plan.schedule.schedule_id,
        )).status, ScheduleStatus.DELIVERED)

    async def test_concurrent_duplicate_ticks_advance_only_once(self):
        started = await self.service.start_activity(self.initial.activity_id)
        event = (await self.plan(started)).schedule.event
        self.now += timedelta(seconds=5)
        first, second = await asyncio.gather(self.service.handle_event(event), self.service.handle_event(event))
        self.assertEqual(first, second)
        self.assertEqual((await self.read()).instance.revision, 2)

    async def test_first_plan_insert_failure_rolls_back_start_and_feedback(self):
        context = ActivityContext(definition=self.definition, instance=self.initial, now=self.now)
        result = await self.manager.start(context)
        self.execute_sql("""CREATE TRIGGER fail_plan BEFORE INSERT ON scheduled_events
            BEGIN SELECT RAISE(ABORT, 'plan failure'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.service.start_activity(self.initial.activity_id)
        self.assertEqual((await self.read()).instance, self.initial)
        self.assertIsNone(await self.storage.get_event(result.events[0].event_id))
        self.execute_sql("DROP TRIGGER fail_plan")
        self.assertEqual((await self.service.start_activity(self.initial.activity_id)).instance.revision, 1)

    async def test_next_plan_insert_failure_rolls_back_step_and_trigger(self):
        started = await self.service.start_activity(self.initial.activity_id)
        event = (await self.plan(started)).schedule.event
        self.now += timedelta(seconds=5)
        self.execute_sql("""CREATE TRIGGER fail_plan BEFORE INSERT ON scheduled_events
            BEGIN SELECT RAISE(ABORT, 'plan failure'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.service.handle_event(event)
        self.assertEqual(await self.read(), started)
        self.assertIsNone(await self.storage.get_event(event.event_id))
        self.execute_sql("DROP TRIGGER fail_plan")
        self.assertEqual((await self.service.handle_event(event)).instance.revision, 2)

    async def test_completion_failure_rolls_back_activity_trigger_and_reward(self):
        started = await self.service.start_activity(self.initial.activity_id)
        event = (await self.plan(started)).schedule.event
        self.now += timedelta(seconds=20)
        self.execute_sql("""CREATE TRIGGER fail_reward BEFORE INSERT ON state_settlements
            BEGIN SELECT RAISE(ABORT, 'reward failure'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.service.handle_event(event)
        self.assertEqual(await self.read(), started)
        self.assertIsNone(await self.storage.get_event(event.event_id))
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)
        self.execute_sql("DROP TRIGGER fail_reward")
        await self.service.handle_event(event)
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).needs.energy, 50)

    async def test_wrong_role_unknown_and_modified_tick_are_rejected(self):
        started = await self.service.start_activity(self.initial.activity_id)
        plan = (await self.plan(started)).schedule
        self.now += timedelta(seconds=5)
        for updates in (
            {"payload": {**plan.event.payload, "agent_id": "other"}},
            {"event_id": "unknown"}, {"source": "other"},
            {"event_type": "SOCIAL.message_received"}, {"provenance": "simulated"},
            {"payload": {**plan.event.payload, "expected_revision": 2}},
        ):
            with self.subTest(updates=updates):
                event = AgentEvent(**{**plan.event.model_dump(), **updates})
                with self.assertRaises((ValueError, ActivityServiceError)):
                    await self.service.handle_event(event)
        self.assertEqual(await self.read(), started)

    async def test_persisted_future_revision_tick_is_rejected(self):
        started = await self.service.start_activity(self.initial.activity_id)
        event = (await self.plan(started)).schedule.event
        future = AgentEvent(**{
            **event.model_dump(), "event_id": "future-tick",
            "payload": {**event.payload, "expected_revision": 100},
        })
        await self.storage.create_schedule(ScheduledEvent(
            schedule_id=future.event_id, agent_id=self.state.agent_id, event=future, due_at=future.timestamp,
        ))
        self.now += timedelta(seconds=5)
        with self.assertRaises(ActivityServiceError):
            await self.service.handle_event(future)
        self.assertEqual(await self.read(), started)

    async def test_persisted_noncanonical_tick_is_rejected(self):
        started = await self.service.start_activity(self.initial.activity_id)
        event = (await self.plan(started)).schedule.event
        changed = AgentEvent(**{**event.model_dump(), "event_id": "changed-tick"})
        await self.storage.create_schedule(ScheduledEvent(
            schedule_id=changed.event_id, agent_id=self.state.agent_id, event=changed, due_at=changed.timestamp,
        ))
        self.now += timedelta(seconds=5)
        with self.assertRaises(ActivityServiceError):
            await self.service.handle_event(changed)
        self.assertEqual(await self.read(), started)

    async def test_early_tick_does_not_advance_or_get_logged(self):
        started = await self.service.start_activity(self.initial.activity_id)
        event = (await self.plan(started)).schedule.event
        with self.assertRaises(ActivityServiceError):
            await self.service.handle_event(event)
        self.assertEqual(await self.read(), started)
        self.assertIsNone(await self.storage.get_event(event.event_id))

    async def test_missing_or_other_role_activity_is_not_accessible(self):
        with self.assertRaises(ActivityNotFoundError):
            await self.service.start_activity("missing")
        other = ActivityService("other", self.storage, self.manager, clock=lambda: self.now)
        with self.assertRaises(ActivityNotFoundError):
            await other.start_activity(self.initial.activity_id)

    async def test_real_and_hybrid_are_rejected_before_executor_invocation(self):
        for mode in (ExecutionMode.REAL, ExecutionMode.HYBRID):
            definition = ActivityDefinition(definition_id=mode.value, name=mode.value, supported_modes=[mode])
            instance = create_activity_instance(
                definition, activity_id=mode.value, agent_id=self.state.agent_id, mode=mode, created_at=self.time,
            )
            await self.storage.create_activity(definition, instance)
            with self.assertRaises(ActivityServiceError):
                await self.service.start_activity(instance.activity_id)

    async def test_illegal_lifecycle_and_bad_clock_are_rejected(self):
        with self.assertRaises(InvalidActivityStatusError):
            await self.service.pause_activity(self.initial.activity_id)
        with self.assertRaises(ValueError):
            ActivityService(self.state.agent_id, self.storage, self.manager, tick_interval_seconds=True)
        with self.assertRaises(ValueError):
            ActivityService(self.state.agent_id, self.storage, self.manager, clock=123)
        service = ActivityService(self.state.agent_id, self.storage, self.manager, clock=lambda: datetime(2026, 10, 31))
        with self.assertRaises(ValueError):
            await service.start_activity(self.initial.activity_id)

    async def test_completed_feedback_replay_through_agent_runtime_does_not_reward_twice(self):
        await self.service.start_activity(self.initial.activity_id)
        self.now += timedelta(seconds=20)
        await self.scheduler.run_due()
        # 取出实际提交的完成反馈，验证跨入口重放不会再次结算。
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT result_json FROM activity_commits WHERE activity_id = ? ORDER BY from_revision DESC LIMIT 1",
                (self.initial.activity_id,),
            ).fetchone()
        event = ActivityExecutionResult.model_validate_json(row[0]).events[0]
        async with AgentRuntime(self.state.agent_id, self.storage, EventRouter(), clock=lambda: self.now) as runtime:
            state = await runtime.submit(event)
        self.assertEqual((state.needs.energy, state.version), (50, 1))

    async def test_empty_effect_completes_without_role_version_change(self):
        definition = ActivityDefinition(
            definition_id="quiet", name="静坐", supported_modes=["simulated"], estimated_duration_seconds=1,
        )
        registry = ExecutorRegistry()
        registry.register("quiet", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        instance = create_activity_instance(
            definition, activity_id="quiet-001", agent_id=self.state.agent_id,
            mode=ExecutionMode.SIMULATED, created_at=self.time,
        )
        await self.storage.create_activity(definition, instance)
        service = ActivityService(self.state.agent_id, self.storage, ActivityManager(registry), clock=lambda: self.now)
        await service.start_activity(instance.activity_id)
        event = build_activity_tick(await self.storage.get_activity(self.state.agent_id, instance.activity_id)).event
        self.now += timedelta(seconds=1)
        self.assertEqual((await service.handle_event(event)).instance.status, ActivityStatus.COMPLETED)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_background_scheduler_drives_activity(self):
        completed = asyncio.Event()

        async def deliver(event):
            result = await self.service.handle_event(event)
            if result.instance.status == ActivityStatus.COMPLETED:
                completed.set()

        await self.service.start_activity(self.initial.activity_id)
        self.scheduler = TimeScheduler(
            self.state.agent_id, self.storage, deliver, clock=lambda: self.now, poll_interval=0.001,
        )
        self.now += timedelta(seconds=10)
        async with self.scheduler:
            await asyncio.wait_for(completed.wait(), timeout=5)
        self.assertEqual((await self.read()).instance.status, ActivityStatus.COMPLETED)

    async def test_storage_rejects_mismatched_followup_and_missing_trigger_plan(self):
        context = ActivityContext(definition=self.definition, instance=self.initial, now=self.now)
        result = await self.manager.start(context)
        record = ActivityRecord(definition=self.definition, instance=result.instance)
        plan = build_activity_tick(record)
        bad = ScheduledEvent(**{**plan.model_dump(), "due_at": plan.due_at + timedelta(seconds=1)})
        with self.assertRaises(ValueError):
            await self.storage.commit_activity_result("start", context, result, next_schedule=bad)
        self.assertEqual((await self.read()).instance, self.initial)
        # 不带计划的旧接口仍可使用，但不会自动成为受管理活动。
        saved = await self.storage.commit_activity_result("start", context, result)
        self.now += timedelta(seconds=5)
        context = ActivityContext(definition=self.definition, instance=saved.instance, now=self.now)
        result = await self.manager.step(context)
        followup = build_activity_tick(ActivityRecord(definition=self.definition, instance=result.instance))
        with self.assertRaises(ValueError):
            await self.storage.commit_activity_result(
                "step", context, result, next_schedule=followup, trigger_event=plan.event,
            )
        self.assertEqual(await self.read(), saved)

    async def test_conflicting_followup_rolls_back_start(self):
        context = ActivityContext(definition=self.definition, instance=self.initial, now=self.now)
        result = await self.manager.start(context)
        plan = build_activity_tick(ActivityRecord(definition=self.definition, instance=result.instance))
        await self.storage.create_schedule(ScheduledEvent(**{**plan.model_dump(), "max_attempts": 1}))
        with self.assertRaises(ScheduleConflictError):
            await self.service.start_activity(self.initial.activity_id)
        self.assertEqual((await self.read()).instance, self.initial)
        self.assertIsNone(await self.storage.get_event(result.events[0].event_id))

    async def test_no_progress_executor_keeps_tick_pending_for_retry(self):
        started = await self.service.start_activity(self.initial.activity_id)
        event = (await self.plan(started)).schedule.event
        self.now += timedelta(seconds=5)
        original = self.manager.step
        self.manager.step = AsyncMock(return_value=ActivityExecutionResult(instance=started.instance))
        try:
            self.assertEqual((await self.scheduler.run_due()).failed, (event.event_id,))
        finally:
            self.manager.step = original
        self.assertEqual(await self.read(), started)
        self.assertEqual((await self.plan(started)).status, ScheduleStatus.PENDING)
        self.assertEqual((await self.plan(started)).attempts, 1)

    async def test_exact_commit_retry_keeps_latest_activity_and_schedule_receipts(self):
        context = ActivityContext(definition=self.definition, instance=self.initial, now=self.now)
        result = await self.manager.start(context)
        plan = build_activity_tick(ActivityRecord(definition=self.definition, instance=result.instance))
        await self.storage.commit_activity_result("start", context, result, next_schedule=plan)
        self.now += timedelta(seconds=5)
        await self.scheduler.run_due()
        latest = await self.read()
        old_receipt = await self.storage.get_schedule(self.state.agent_id, plan.schedule_id)
        self.assertEqual(await self.storage.commit_activity_result(
            "start", context, result, next_schedule=plan,
        ), latest)
        self.assertEqual(await self.storage.get_schedule(self.state.agent_id, plan.schedule_id), old_receipt)

    async def record_failure(self, plan, attempt):
        self.now = self.time + timedelta(seconds=5 * (attempt + 1))
        return await self.storage.record_schedule_delivery(
            self.state.agent_id, plan.schedule_id, expected_attempts=attempt,
            attempted_at=self.now, succeeded=False, error="test delivery failure",
        )

    def legacy_failed_plan(self, plan):
        # 模拟修复前遗留数据：计划已耗尽，活动仍保持 running。
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE scheduled_events SET status = 'failed', attempts = ?, next_attempt_at = ?, "
                "last_error = ? WHERE schedule_id = ?",
                (plan.max_attempts, (self.time + timedelta(seconds=15)).isoformat(timespec="microseconds"),
                 "legacy delivery failure", plan.schedule_id),
            )
            connection.commit()

    async def test_exhausted_no_progress_tick_atomically_fails_activity(self):
        started = await self.service.start_activity(self.initial.activity_id)
        plan = (await self.plan(started)).schedule
        original = self.manager.step
        self.manager.step = AsyncMock(return_value=ActivityExecutionResult(instance=started.instance))
        try:
            for attempt in range(3):
                self.now = self.time + timedelta(seconds=5 * (attempt + 1))
                self.assertEqual((await self.scheduler.run_due()).failed, (plan.schedule_id,))
                if attempt < 2:
                    self.assertEqual(await self.read(), started)
        finally:
            self.manager.step = original
        failed_plan = await self.storage.get_schedule(self.state.agent_id, plan.schedule_id)
        self.assertEqual((failed_plan.status, failed_plan.attempts), (ScheduleStatus.FAILED, 3))
        failed = await self.read()
        self.assertEqual(failed.instance.status, ActivityStatus.FAILED)
        self.assertEqual(failed.instance.revision, started.instance.revision + 1)
        self.assertEqual(failed.instance.progress, started.instance.progress)
        context, result = build_tick_exhaustion_result(started, failed_plan)
        self.assertEqual(failed.instance, result.instance)
        self.assertEqual(await self.storage.get_event(result.events[0].event_id), result.events[0])
        self.assertEqual(result.events[0].causation_id, plan.event.event_id)
        self.assertEqual(result.events[0].payload["reason"], "tick_attempts_exhausted")
        self.assertEqual(context.now, self.now)
        self.assertEqual(await self.storage.get_event(plan.event.event_id), plan.event)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)
        self.assertEqual((await self.scheduler.run_due()).delivered, ())
        self.assertEqual(await self.read(), failed)

    async def test_transient_tick_failure_recovers_on_last_attempt(self):
        started = await self.service.start_activity(self.initial.activity_id)
        plan = (await self.plan(started)).schedule
        original = self.manager.step
        calls = 0

        async def step(context):
            nonlocal calls
            calls += 1
            if calls < 3:
                return ActivityExecutionResult(instance=context.instance)
            return await original(context)

        self.manager.step = step
        try:
            for attempt in range(3):
                self.now = self.time + timedelta(seconds=5 * (attempt + 1))
                await self.scheduler.run_due()
        finally:
            self.manager.step = original
        self.assertEqual((await self.read()).instance.status, ActivityStatus.COMPLETED)
        saved = await self.storage.get_schedule(self.state.agent_id, plan.schedule_id)
        self.assertEqual((saved.status, saved.attempts), (ScheduleStatus.DELIVERED, 3))
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).needs.energy, 50)

    async def test_exhausted_executor_exception_also_fails_activity(self):
        started = await self.service.start_activity(self.initial.activity_id)
        original = self.manager.step
        self.manager.step = AsyncMock(side_effect=RuntimeError("executor unavailable"))
        try:
            for attempt in range(3):
                self.now = self.time + timedelta(seconds=5 * (attempt + 1))
                await self.scheduler.run_due()
        finally:
            self.manager.step = original
        failed_plan = await self.plan(started)
        _, result = build_tick_exhaustion_result(started, failed_plan)
        self.assertEqual((await self.read()).instance.status, ActivityStatus.FAILED)
        self.assertIn("executor unavailable", result.events[0].payload["last_error"])
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_exhausted_old_tick_cannot_fail_new_running_revision(self):
        started = await self.service.start_activity(self.initial.activity_id)
        old_plan = (await self.plan(started)).schedule
        self.now += timedelta(seconds=5)
        newer = await self.service.handle_event(old_plan.event)
        next_plan = await self.plan(newer)
        # 接收方已推进，但包装回调随后持续报错，不能把较新的活动判为失败。
        for attempt in range(3):
            await self.record_failure(old_plan, attempt)
        self.assertEqual(await self.read(), newer)
        self.assertEqual(await self.plan(newer), next_plan)
        await self.storage.close()
        await self.storage.open()
        self.assertEqual(await self.read(), newer)
        self.assertEqual(await self.plan(newer), next_plan)

    async def test_exhausted_old_tick_cannot_overwrite_pause(self):
        started = await self.service.start_activity(self.initial.activity_id)
        old_plan = (await self.plan(started)).schedule
        await self.record_failure(old_plan, 0)
        self.now += timedelta(seconds=1)
        paused = await self.service.pause_activity(self.initial.activity_id)
        for attempt in (1, 2):
            await self.record_failure(old_plan, attempt)
        self.assertEqual((await self.storage.get_schedule(
            self.state.agent_id, old_plan.schedule_id,
        )).status, ScheduleStatus.FAILED)
        self.assertEqual(await self.read(), paused)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_exhaustion_failure_rolls_back_plan_counter_and_activity(self):
        started = await self.service.start_activity(self.initial.activity_id)
        plan = (await self.plan(started)).schedule
        await self.record_failure(plan, 0)
        before_final = await self.record_failure(plan, 1)
        self.execute_sql("""CREATE TRIGGER fail_terminal_commit BEFORE INSERT ON activity_commits
            BEGIN SELECT RAISE(ABORT, 'terminal commit failure'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.record_failure(plan, 2)
        self.assertEqual(await self.storage.get_schedule(self.state.agent_id, plan.schedule_id), before_final)
        self.assertEqual(await self.read(), started)
        self.assertIsNone(await self.storage.get_event(plan.event.event_id))
        self.execute_sql("DROP TRIGGER fail_terminal_commit")
        self.assertEqual((await self.record_failure(plan, 2)).status, ScheduleStatus.FAILED)
        self.assertEqual((await self.read()).instance.status, ActivityStatus.FAILED)

    async def test_reopen_repairs_legacy_exhausted_tick_once(self):
        started = await self.service.start_activity(self.initial.activity_id)
        plan = (await self.plan(started)).schedule
        self.legacy_failed_plan(plan)
        self.assertEqual(await self.read(), started)
        await self.storage.close()
        await self.storage.open()
        failed = await self.read()
        failed_plan = await self.storage.get_schedule(self.state.agent_id, plan.schedule_id)
        _, result = build_tick_exhaustion_result(started, failed_plan)
        self.assertEqual(failed.instance, result.instance)
        self.assertEqual(failed.instance.updated_at, self.time + timedelta(seconds=15))
        self.assertEqual(await self.storage.get_event(result.events[0].event_id), result.events[0])
        await self.storage.close()
        await self.storage.open()
        self.assertEqual(await self.read(), failed)
        self.assertEqual(await self.storage.get_schedule(self.state.agent_id, plan.schedule_id), failed_plan)
        self.assertEqual(await self.storage.get_state(self.state.agent_id), self.state)

    async def test_failed_reopen_repair_rolls_back_and_can_retry(self):
        started = await self.service.start_activity(self.initial.activity_id)
        plan = (await self.plan(started)).schedule
        self.legacy_failed_plan(plan)
        self.execute_sql("""CREATE TRIGGER fail_terminal_commit BEFORE INSERT ON activity_commits
            BEGIN SELECT RAISE(ABORT, 'repair failure'); END;""")
        await self.storage.close()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.storage.open()
        with closing(sqlite3.connect(self.path)) as connection:
            saved = connection.execute(
                "SELECT instance_json FROM activity_instances WHERE activity_id = ?", (self.initial.activity_id,),
            ).fetchone()[0]
            self.assertEqual(saved, started.instance.model_dump_json())
            self.assertIsNone(connection.execute(
                "SELECT event_id FROM agent_events WHERE event_id = ?", (plan.event.event_id,),
            ).fetchone())
        self.execute_sql("DROP TRIGGER fail_terminal_commit")
        await self.storage.open()
        self.assertEqual((await self.read()).instance.status, ActivityStatus.FAILED)

    async def test_exhausted_unrelated_and_noncanonical_plans_cannot_fail_activity(self):
        started = await self.service.start_activity(self.initial.activity_id)
        active_plan = (await self.plan(started)).schedule
        for identifier, event_type in (("unrelated", "INTERNAL.other_tick"), ("forged", "INTERNAL.activity_tick")):
            with self.subTest(identifier=identifier):
                event = AgentEvent(**{
                    **active_plan.event.model_dump(), "event_id": identifier, "event_type": event_type,
                })
                plan = ScheduledEvent(
                    schedule_id=identifier, agent_id=self.state.agent_id, event=event, due_at=event.timestamp,
                )
                await self.storage.create_schedule(plan)
                for attempt in range(3):
                    await self.record_failure(plan, attempt)
                self.assertEqual(await self.read(), started)
        await self.storage.close()
        await self.storage.open()
        self.assertEqual(await self.read(), started)
        self.assertEqual((await self.plan(started)).status, ScheduleStatus.PENDING)
