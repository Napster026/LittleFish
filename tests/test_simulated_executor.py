"""执行器协议与模拟生命周期测试，由开发者手动运行。"""

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from activity.executor import (
    ActivityContext, ActivityExecutionError, ActivityExecutionResult,
    ActivityExecutor, InvalidActivityStatusError,
)
from activity.executors.simulated import SimulatedActivityExecutor
from activity.models import (
    ActivityDefinition, ActivityInstance, ActivityStatus, ExecutionMode, create_activity_instance,
)
from core.events import AgentEvent, EventProvenance


class SimulatedExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.executor = SimulatedActivityExecutor()
        self.time = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.definition = ActivityDefinition(
            definition_id="rest", name="休息", supported_modes=["simulated"],
            estimated_duration_seconds=10, completion_needs_delta={"energy": 10},
        )
        self.initial = create_activity_instance(
            self.definition, activity_id="activity-001", agent_id="persona-001",
            mode=ExecutionMode.SIMULATED, created_at=self.time,
        )

    def context(self, instance=None, seconds=0, definition=None):
        return ActivityContext(
            definition=self.definition if definition is None else definition,
            instance=self.initial if instance is None else instance,
            now=self.time + timedelta(seconds=seconds),
        )

    async def started(self):
        return (await self.executor.start(self.context())).instance

    async def test_protocol_and_capabilities(self):
        self.assertIsInstance(self.executor, ActivityExecutor)
        self.assertTrue(self.executor.capabilities.supports_pause)
        self.assertTrue(self.executor.capabilities.supports_resume)
        self.assertTrue(self.executor.capabilities.supports_snapshot)

    async def test_context_json_round_trip(self):
        context = self.context()
        restored = ActivityContext.model_validate_json(context.model_dump_json())
        self.assertEqual(restored, context)

    async def test_invalid_context_is_rejected(self):
        original = self.context().model_dump()
        for field, value in (
            ("now", self.time - timedelta(seconds=1)),
            ("now", datetime(2026, 10, 31)),
            ("now", 123),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError):
                    ActivityContext(**{**original, field: value})
        data = self.initial.model_dump()
        data["definition_id"] = "other"
        with self.assertRaises(ValidationError):
            self.context(ActivityInstance(**data))
        data = self.initial.model_dump()
        data["mode"] = "real"
        with self.assertRaises(ValidationError):
            self.context(ActivityInstance(**data))

    async def test_start_returns_running_state_and_simulated_event(self):
        result = await self.executor.start(self.context())
        self.assertEqual(result.instance.status, ActivityStatus.RUNNING)
        self.assertEqual(result.instance.progress, 0)
        self.assertEqual(result.instance.revision, 1)
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.event_type, "ACTIVITY.started")
        self.assertEqual(event.activity_id, self.initial.activity_id)
        self.assertEqual(event.provenance, EventProvenance.SIMULATED)
        self.assertEqual(event.payload["agent_id"], self.initial.agent_id)

    async def test_unsupported_configuration_is_rejected(self):
        data = self.definition.model_dump()
        data["estimated_duration_seconds"] = None
        with self.assertRaises(ActivityExecutionError):
            await self.executor.start(self.context(definition=ActivityDefinition(**data)))
        data = self.definition.model_dump()
        data["supported_modes"] = ["simulated", "real", "hybrid"]
        definition = ActivityDefinition(**data)
        for mode in (ExecutionMode.REAL, ExecutionMode.HYBRID):
            instance = create_activity_instance(
                definition, activity_id="activity-real", agent_id="persona-001",
                mode=mode, created_at=self.time,
            )
            with self.subTest(mode=mode):
                with self.assertRaises(ActivityExecutionError):
                    await self.executor.start(self.context(instance, definition=definition))
        data = self.initial.model_dump()
        data.update(status="running", progress=None)
        with self.assertRaises(ActivityExecutionError):
            await self.executor.step(self.context(ActivityInstance(**data), seconds=1))

    async def test_step_advances_time_based_progress(self):
        result = await self.executor.step(self.context(await self.started(), seconds=3))
        self.assertAlmostEqual(result.instance.progress, 0.3)
        self.assertEqual(result.instance.status, ActivityStatus.RUNNING)
        self.assertEqual(result.instance.revision, 2)
        self.assertEqual(result.events[0].event_type, "ACTIVITY.progressed")
        instance = await self.started()
        for seconds in range(1, 11):
            instance = (await self.executor.step(self.context(instance, seconds=seconds))).instance
        self.assertEqual(instance.status, ActivityStatus.COMPLETED)

    async def test_completion_includes_effect_template(self):
        result = await self.executor.step(self.context(await self.started(), seconds=12))
        self.assertEqual(result.instance.status, ActivityStatus.COMPLETED)
        self.assertEqual(result.instance.progress, 1)
        self.assertEqual(result.events[0].event_type, "ACTIVITY.completed")
        self.assertEqual(result.events[0].payload["needs_delta"]["energy"], 10)
        self.assertEqual(
            ActivityExecutionResult.model_validate_json(result.model_dump_json()), result,
        )

    async def test_activity_without_effect_does_not_invent_one(self):
        data = self.definition.model_dump()
        data["completion_needs_delta"] = None
        definition = ActivityDefinition(**data)
        started = await self.executor.start(self.context(definition=definition))
        completed = await self.executor.step(self.context(started.instance, 10, definition))
        self.assertNotIn("needs_delta", completed.events[0].payload)

    async def test_no_elapsed_time_does_not_advance_or_emit_event(self):
        for duration in (10, 1e-12):
            data = self.definition.model_dump()
            data["estimated_duration_seconds"] = duration
            definition = ActivityDefinition(**data)
            started = await self.executor.start(self.context(definition=definition))
            result = await self.executor.step(self.context(started.instance, definition=definition))
            self.assertEqual(result.instance, started.instance)
            self.assertEqual(result.events, ())

    async def test_pause_settles_elapsed_running_time(self):
        result = await self.executor.pause(self.context(await self.started(), seconds=3))
        self.assertEqual(result.instance.status, ActivityStatus.PAUSED)
        self.assertAlmostEqual(result.instance.progress, 0.3)
        self.assertEqual(result.events[0].event_type, "ACTIVITY.paused")

    async def test_resume_excludes_paused_time(self):
        paused = await self.executor.pause(self.context(await self.started(), seconds=2))
        resumed = await self.executor.resume(self.context(paused.instance, seconds=50))
        self.assertAlmostEqual(resumed.instance.progress, 0.2)
        self.assertEqual(resumed.events[0].event_type, "ACTIVITY.resumed")
        progressed = await self.executor.step(self.context(resumed.instance, seconds=53))
        self.assertAlmostEqual(progressed.instance.progress, 0.5)
        completed = await self.executor.step(self.context(progressed.instance, seconds=58))
        self.assertEqual(completed.instance.status, ActivityStatus.COMPLETED)

    async def test_stop_cancels_unfinished_activities(self):
        running = await self.started()
        paused = (await self.executor.pause(self.context(running, seconds=1))).instance
        for instance, expected_progress in ((self.initial, 0), (running, 0.2), (paused, 0.1)):
            with self.subTest(status=instance.status):
                result = await self.executor.stop(self.context(instance, seconds=2))
                self.assertEqual(result.instance.status, ActivityStatus.CANCELLED)
                self.assertAlmostEqual(result.instance.progress, expected_progress)
                self.assertEqual(result.events[0].event_type, "ACTIVITY.cancelled")
                self.assertNotIn("needs_delta", result.events[0].payload)

    async def test_pause_or_stop_at_completion_returns_completed(self):
        running = await self.started()
        for operation in (self.executor.pause, self.executor.stop):
            with self.subTest(operation=operation.__name__):
                result = await operation(self.context(running, seconds=10))
                self.assertEqual(result.instance.status, ActivityStatus.COMPLETED)
                self.assertEqual(result.events[0].event_type, "ACTIVITY.completed")

    async def test_stopping_terminal_activity_is_a_noop(self):
        completed = (await self.executor.step(self.context(await self.started(), seconds=10))).instance
        for status in (ActivityStatus.COMPLETED, ActivityStatus.FAILED, ActivityStatus.CANCELLED):
            instance = ActivityInstance(**{**completed.model_dump(), "status": status})
            with self.subTest(status=status):
                result = await self.executor.stop(self.context(instance, seconds=20))
                self.assertEqual(result.instance, instance)
                self.assertEqual(result.events, ())

    async def test_illegal_lifecycle_operations_are_rejected(self):
        running = await self.started()
        for operation, instance in (
            (self.executor.start, running), (self.executor.step, self.initial),
            (self.executor.pause, self.initial), (self.executor.resume, running),
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(InvalidActivityStatusError):
                    await operation(self.context(instance, seconds=1))
        paused = (await self.executor.pause(self.context(running, seconds=1))).instance
        with self.assertRaises(InvalidActivityStatusError):
            await self.executor.step(self.context(paused, seconds=2))

    async def test_observe_and_snapshot_are_read_only_and_restorable(self):
        running = await self.started()
        observed = await self.executor.observe(self.context(running, seconds=3))
        self.assertEqual(observed.instance, running)
        self.assertEqual(observed.events, ())
        snapshot = await self.executor.snapshot(self.context(running, seconds=3))
        restored = ActivityInstance.model_validate_json(snapshot.model_dump_json())
        result = await SimulatedActivityExecutor().step(self.context(restored, seconds=5))
        self.assertAlmostEqual(result.instance.progress, 0.5)

    async def test_event_identity_is_stable_and_revision_distinguishes_repeated_actions(self):
        first = await self.executor.start(self.context())
        retry = await self.executor.start(self.context())
        self.assertEqual(first, retry)
        paused = await self.executor.pause(self.context(first.instance))
        resumed = await self.executor.resume(self.context(paused.instance))
        paused_again = await self.executor.pause(self.context(resumed.instance))
        self.assertNotEqual(paused.events[0].event_id, paused_again.events[0].event_id)
        self.assertEqual(paused_again.instance.revision, 4)

    async def test_inputs_are_not_modified(self):
        initial_before = self.initial.model_dump_json()
        definition_before = self.definition.model_dump_json()
        started = await self.executor.start(self.context())
        running_before = started.instance.model_dump_json()
        await self.executor.step(self.context(started.instance, seconds=3))
        self.assertEqual(self.initial.model_dump_json(), initial_before)
        self.assertEqual(self.definition.model_dump_json(), definition_before)
        self.assertEqual(started.instance.model_dump_json(), running_before)

    async def test_result_rejects_events_for_another_activity_or_agent(self):
        result = await self.executor.start(self.context())
        data = result.events[0].model_dump()
        data["activity_id"] = "other"
        with self.assertRaises(ValidationError):
            ActivityExecutionResult(instance=result.instance, events=(AgentEvent(**data),))
        data = result.events[0].model_dump()
        data["payload"]["agent_id"] = "other"
        with self.assertRaises(ValidationError):
            ActivityExecutionResult(instance=result.instance, events=(AgentEvent(**data),))
