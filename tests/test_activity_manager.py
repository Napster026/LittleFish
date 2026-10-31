"""执行器注册与活动编排测试，由开发者手动运行。"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from persona_agent.activity.executor import (
    ActivityContext, ActivityExecutionError, ActivityExecutionResult,
    ExecutorCapabilities, InvalidActivityStatusError,
)
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.manager import (
    ActivityManager, InvalidExecutorResultError, UnsupportedExecutorCapabilityError,
)
from persona_agent.activity.models import (
    ActivityDefinition, ActivityInstance, ActivityStatus, ExecutionMode, create_activity_instance,
)
from persona_agent.activity.registry import (
    DuplicateExecutorError, ExecutorNotFoundError, ExecutorRegistry, InvalidExecutorError,
)
from persona_agent.core.events import AgentEvent


class ActivityManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.time = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.definition = ActivityDefinition(
            definition_id="rest", name="休息", supported_modes=["simulated"],
            estimated_duration_seconds=10, completion_needs_delta={"energy": 10},
        )
        self.initial = create_activity_instance(
            self.definition, activity_id="activity-001", agent_id="persona-001",
            mode=ExecutionMode.SIMULATED, created_at=self.time,
        )
        self.executor = SimulatedActivityExecutor()
        self.registry = ExecutorRegistry()
        self.registry.register("rest", ExecutionMode.SIMULATED, self.executor)
        self.manager = ActivityManager(self.registry)

    def context(self, instance=None, seconds=0):
        return ActivityContext(
            definition=self.definition, instance=self.initial if instance is None else instance,
            now=self.time + timedelta(seconds=seconds),
        )

    async def test_registry_resolves_exact_definition_and_mode(self):
        self.assertIs(self.registry.resolve(self.definition, ExecutionMode.SIMULATED), self.executor)
        other_definition = ActivityDefinition(
            definition_id="draw", name="画画", supported_modes=["simulated", "real"],
        )
        second, third = SimulatedActivityExecutor(), SimulatedActivityExecutor()
        self.registry.register("draw", ExecutionMode.SIMULATED, second)
        # 此处仅验证精确键，注册表不会替执行器保证它实际支持某模式。
        self.registry.register("draw", ExecutionMode.REAL, third)
        self.assertIs(self.registry.resolve(other_definition, ExecutionMode.SIMULATED), second)
        self.assertIs(self.registry.resolve(other_definition, ExecutionMode.REAL), third)

    async def test_registry_does_not_replace_duplicate(self):
        with self.assertRaises(DuplicateExecutorError):
            self.registry.register("rest", ExecutionMode.SIMULATED, SimulatedActivityExecutor())
        self.assertIs(self.registry.resolve(self.definition, ExecutionMode.SIMULATED), self.executor)

    async def test_missing_or_unsupported_mode_does_not_fall_back(self):
        with self.assertRaises(ActivityExecutionError):
            self.registry.resolve(self.definition, ExecutionMode.REAL)
        definition = ActivityDefinition(
            definition_id="watch", name="视频", supported_modes=["simulated", "real"],
        )
        with self.assertRaises(ExecutorNotFoundError):
            self.registry.resolve(definition, ExecutionMode.REAL)

    async def test_invalid_registration_is_rejected(self):
        for definition_id in ("", " rest", 123):
            with self.subTest(definition_id=definition_id):
                with self.assertRaises(ValueError):
                    self.registry.register(definition_id, ExecutionMode.SIMULATED, self.executor)
        with self.assertRaises(ValueError):
            self.registry.register("rest", "unknown", self.executor)
        with self.assertRaises(InvalidExecutorError):
            self.registry.register("other", ExecutionMode.SIMULATED, None)

    async def test_manager_runs_full_simulated_lifecycle(self):
        started = await self.manager.start(self.context())
        paused = await self.manager.pause(self.context(started.instance, 2))
        resumed = await self.manager.resume(self.context(paused.instance, 50))
        progressed = await self.manager.step(self.context(resumed.instance, 53))
        self.assertAlmostEqual(progressed.instance.progress, 0.5)
        completed = await self.manager.step(self.context(progressed.instance, 58))
        self.assertEqual(completed.instance.status, ActivityStatus.COMPLETED)
        self.assertEqual(completed.events[0].payload["needs_delta"]["energy"], 10)
        stopped = await self.manager.stop(self.context(completed.instance, 60))
        self.assertEqual(stopped.instance, completed.instance)
        self.assertEqual(stopped.events, ())

    async def test_unsupported_capability_is_rejected_before_call(self):
        class LimitedExecutor(SimulatedActivityExecutor):
            @property
            def capabilities(self):
                return ExecutorCapabilities()

        limited = LimitedExecutor()
        limited.pause = AsyncMock()
        limited.resume = AsyncMock()
        limited.snapshot = AsyncMock()
        registry = ExecutorRegistry()
        registry.register("rest", ExecutionMode.SIMULATED, limited)
        manager = ActivityManager(registry)
        running = (await self.executor.start(self.context())).instance
        paused = (await self.executor.pause(self.context(running, 1))).instance
        for operation, context in (
            (manager.pause, self.context(running, 1)),
            (manager.resume, self.context(paused, 2)),
            (manager.snapshot, self.context(running, 1)),
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(UnsupportedExecutorCapabilityError):
                    await operation(context)
        limited.pause.assert_not_called()
        limited.resume.assert_not_called()
        limited.snapshot.assert_not_called()

    async def test_input_lifecycle_is_checked_before_execution(self):
        running = (await self.executor.start(self.context())).instance
        for operation, instance in (
            (self.manager.start, running), (self.manager.step, self.initial),
            (self.manager.pause, self.initial), (self.manager.resume, running),
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(InvalidActivityStatusError):
                    await operation(self.context(instance, 1))

    async def test_wrong_result_type_non_awaitable_and_missing_feedback_are_rejected(self):
        self.executor.start = AsyncMock(return_value={"status": "running"})
        with self.assertRaises(InvalidExecutorResultError):
            await self.manager.start(self.context())
        self.executor.start = lambda context: ActivityExecutionResult(instance=context.instance)
        with self.assertRaises(InvalidExecutorResultError):
            await self.manager.start(self.context())
        candidate = ActivityInstance(**{
            **self.initial.model_dump(), "status": "running", "revision": 1,
        })
        self.executor.start = AsyncMock(return_value=ActivityExecutionResult(instance=candidate))
        with self.assertRaises(InvalidExecutorResultError):
            await self.manager.start(self.context())

    async def test_identity_fields_cannot_be_changed(self):
        base = (await self.executor.start(self.context())).instance.model_dump()
        for field, value in (
            ("activity_id", "other"), ("definition_id", "other"), ("agent_id", "other"),
            ("mode", "real"), ("created_at", self.time - timedelta(seconds=1)),
        ):
            with self.subTest(field=field):
                candidate = ActivityInstance(**{**base, field: value})
                self.executor.start = AsyncMock(return_value=ActivityExecutionResult(instance=candidate))
                with self.assertRaises(InvalidExecutorResultError):
                    await self.manager.start(self.context())

    async def test_revision_and_time_cannot_skip_or_move_backwards(self):
        base = ActivityInstance(**{
            **self.initial.model_dump(), "updated_at": self.time + timedelta(seconds=5),
        })
        for revision, updated_at in (
            (0, base.updated_at), (2, base.updated_at), (1, self.time + timedelta(seconds=4)),
        ):
            with self.subTest(revision=revision, updated_at=updated_at):
                candidate = ActivityInstance(**{
                    **base.model_dump(), "status": "running", "revision": revision,
                    "updated_at": updated_at,
                })
                self.executor.start = AsyncMock(return_value=ActivityExecutionResult(instance=candidate))
                with self.assertRaises(InvalidExecutorResultError):
                    await self.manager.start(self.context(base, 5))

    async def test_output_lifecycle_must_match_operation(self):
        candidate = ActivityInstance(**{
            **self.initial.model_dump(), "status": "paused", "revision": 1,
        })
        self.executor.start = AsyncMock(return_value=ActivityExecutionResult(instance=candidate))
        with self.assertRaises(InvalidExecutorResultError):
            await self.manager.start(self.context())

    async def test_terminal_state_and_snapshot_cannot_be_modified(self):
        running = (await self.executor.start(self.context())).instance
        completed = (await self.executor.step(self.context(running, 10))).instance
        candidate = ActivityInstance(**{
            **completed.model_dump(), "status": "cancelled", "revision": completed.revision + 1,
        })
        self.executor.observe = AsyncMock(return_value=ActivityExecutionResult(instance=candidate))
        with self.assertRaises(InvalidExecutorResultError):
            await self.manager.observe(self.context(completed, 11))
        self.executor.snapshot = AsyncMock(return_value=candidate)
        with self.assertRaises(InvalidExecutorResultError):
            await self.manager.snapshot(self.context(completed, 11))

    async def test_feedback_metadata_and_simulated_source_are_checked(self):
        result = await self.executor.start(self.context())
        for field, value in (("definition_id", "other"), ("mode", "real"), ("revision", 2), ("revision", True)):
            with self.subTest(field=field, value=value):
                data = result.events[0].model_dump()
                data["payload"][field] = value
                candidate = ActivityExecutionResult(instance=result.instance, events=(AgentEvent(**data),))
                self.executor.start = AsyncMock(return_value=candidate)
                with self.assertRaises(InvalidExecutorResultError):
                    await self.manager.start(self.context())
        data = result.events[0].model_dump()
        data["provenance"] = "observed"
        candidate = ActivityExecutionResult(instance=result.instance, events=(AgentEvent(**data),))
        self.executor.start = AsyncMock(return_value=candidate)
        with self.assertRaises(InvalidExecutorResultError):
            await self.manager.start(self.context())

    async def test_executor_receives_independent_validated_context(self):
        context = self.context()
        before = context.model_dump_json()
        original_start = self.executor.start

        async def handler(received):
            self.assertIsNot(received, context)
            self.assertIsNot(received.instance, context.instance)
            return await original_start(received)

        self.executor.start = handler
        await self.manager.start(context)
        self.assertEqual(context.model_dump_json(), before)

    async def test_executor_error_reaches_caller(self):
        self.executor.start = AsyncMock(side_effect=RuntimeError("executor failure"))
        with self.assertRaisesRegex(RuntimeError, "executor failure"):
            await self.manager.start(self.context())

    async def test_observe_and_snapshot_do_not_advance_simulated_activity(self):
        running = (await self.manager.start(self.context())).instance
        observed = await self.manager.observe(self.context(running, 5))
        snapshot = await self.manager.snapshot(self.context(running, 5))
        self.assertEqual(observed.instance, running)
        self.assertEqual(observed.events, ())
        self.assertEqual(snapshot, running)
