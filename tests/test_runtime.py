"""单角色运行时测试，使用临时数据库，由开发者手动运行。"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.events import AgentEvent
from core.router import EventRouter
from core.runtime import (
    AgentRuntime, AgentRuntimeError, RuntimeNotRunningError,
    RuntimeQueueFullError, RuntimeStoppingError,
)
from core.settlement import StateChangeProposal
from core.state import AgentState
from infrastructure.storage import SQLiteStorage
from application.persistence import PersonaStore
from persistence.errors import (
    EventConflictError,
    StateNotFoundError,
)
from persona.needs import NeedState


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.storage = PersonaStore(SQLiteStorage(Path(self.directory.name) / "agent.sqlite3"))
        await self.storage.open()
        self.time = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.state = AgentState(
            agent_id="persona-001", updated_at=self.time,
            needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
        )
        await self.storage.create_state(self.state)
        self.router = EventRouter()
        self.handled = []
        self.runtimes = []

        async def handler(event, state):
            self.handled.append((event.event_id, state.version))
            return self.proposal(event, state)

        self.router.register("ACTIVITY.completed", handler)

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.stop()
        await self.storage.close()
        self.directory.cleanup()

    def make_runtime(self, router=None, **updates):
        options = dict(clock=lambda: self.time + timedelta(seconds=1))
        options.update(updates)
        runtime = AgentRuntime(
            self.state.agent_id, self.storage,
            self.router if router is None else router, **options,
        )
        self.runtimes.append(runtime)
        return runtime

    def event(self, event_id="event-001", **updates):
        data = dict(
            event_id=event_id, event_type="ACTIVITY.completed", timestamp=self.time,
            source="activity.simulated", provenance="simulated",
            payload={"definition_id": "rest"},
        )
        data.update(updates)
        return AgentEvent(**data)

    def proposal(self, event, state):
        return StateChangeProposal(
            proposal_id=f"proposal:{event.event_id}", event_id=event.event_id,
            agent_id=state.agent_id, expected_version=state.version,
            needs_delta={"energy": 10},
        )

    async def test_submit_requires_running_runtime(self):
        runtime = self.make_runtime()
        with self.assertRaises(RuntimeNotRunningError):
            await runtime.submit(self.event())

    async def test_context_manages_lifecycle_and_commits_state(self):
        runtime = self.make_runtime()
        async with runtime:
            self.assertTrue(runtime.is_running)
            result = await runtime.submit(self.event())
            self.assertEqual(result.needs.energy, 50)
            self.assertEqual(result.version, 1)
            self.assertEqual(await self.storage.get_state(self.state.agent_id), result)
        self.assertFalse(runtime.is_running)
        # 运行时不拥有数据库连接，停止后仍可读取。
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).version, 1)

    async def test_concurrent_submissions_are_processed_in_order(self):
        async with self.make_runtime() as runtime:
            first, second = await asyncio.gather(
                runtime.submit(self.event("event-001")),
                runtime.submit(self.event("event-002")),
            )
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        self.assertEqual(second.needs.energy, 60)
        self.assertEqual(self.handled, [("event-001", 0), ("event-002", 1)])

    async def test_retry_after_reopen_skips_handler_and_returns_latest_state(self):
        async with self.make_runtime() as runtime:
            await runtime.submit(self.event())
            latest = await runtime.submit(self.event("event-002"))
        await self.storage.close()
        await self.storage.open()
        async with self.make_runtime() as restarted:
            self.assertEqual(await restarted.submit(self.event()), latest)
        self.assertEqual(len(self.handled), 2)
        self.assertEqual(latest.version, 2)

    async def test_conflicting_event_does_not_stop_worker(self):
        async with self.make_runtime() as runtime:
            await runtime.submit(self.event())
            with self.assertRaises(EventConflictError):
                await runtime.submit(self.event(payload={"definition_id": "changed"}))
            self.assertTrue(runtime.is_running)
            result = await runtime.submit(self.event("event-002"))
            self.assertEqual(result.version, 2)

    async def test_handler_failure_allows_later_events_and_explicit_retry(self):
        router = EventRouter()
        failed = False

        async def handler(event, state):
            nonlocal failed
            if event.event_id == "event-bad" and not failed:
                failed = True
                raise RuntimeError("handler failure")
            return self.proposal(event, state)

        router.register("ACTIVITY.completed", handler)
        async with self.make_runtime(router) as runtime:
            with self.assertRaisesRegex(RuntimeError, "handler failure"):
                await runtime.submit(self.event("event-bad"))
            self.assertTrue(runtime.is_running)
            self.assertIsNone(await self.storage.get_committed_proposal(self.state.agent_id, "event-bad"))
            self.assertEqual((await runtime.submit(self.event("event-good"))).version, 1)
            self.assertEqual((await runtime.submit(self.event("event-bad"))).version, 2)

    async def test_unregistered_event_is_recorded_without_state_change(self):
        event = self.event(event_type="ACTIVITY.started")
        async with self.make_runtime() as runtime:
            self.assertEqual(await runtime.submit(event), self.state)
        self.assertEqual(await self.storage.get_event(event.event_id), event)

    async def test_none_result_is_not_a_committed_proposal(self):
        router = EventRouter()
        calls = []

        async def handler(event, state):
            calls.append(event.event_id)
            return None

        router.register("ACTIVITY.completed", handler)
        async with self.make_runtime(router) as runtime:
            self.assertEqual(await runtime.submit(self.event()), self.state)
            self.assertEqual(await runtime.submit(self.event()), self.state)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(await self.storage.get_committed_proposal(self.state.agent_id, "event-001"))

    async def test_missing_role_rejects_start(self):
        runtime = AgentRuntime("missing", self.storage, self.router)
        self.runtimes.append(runtime)
        with self.assertRaises(StateNotFoundError):
            await runtime.start()
        self.assertFalse(runtime.is_running)

    async def test_stop_drains_accepted_events_and_rejects_new_ones(self):
        entered, release = asyncio.Event(), asyncio.Event()
        router = EventRouter()

        async def handler(event, state):
            entered.set()
            await release.wait()
            return self.proposal(event, state)

        router.register("ACTIVITY.completed", handler)
        runtime = self.make_runtime(router)
        await runtime.start()
        first = asyncio.create_task(runtime.submit(self.event()))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(runtime.submit(self.event("event-002")))
        await asyncio.sleep(0)
        stopping = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        try:
            self.assertFalse(runtime.is_running)
            with self.assertRaises(RuntimeStoppingError):
                await runtime.submit(self.event("event-003"))
        finally:
            release.set()
            await asyncio.gather(first, second, stopping)
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).version, 2)

    async def test_full_queue_rejects_event_before_recording(self):
        entered, release = asyncio.Event(), asyncio.Event()
        router = EventRouter()

        async def handler(event, state):
            entered.set()
            await release.wait()
            return self.proposal(event, state)

        router.register("ACTIVITY.completed", handler)
        async with self.make_runtime(router, max_pending=1) as runtime:
            first = asyncio.create_task(runtime.submit(self.event()))
            await asyncio.wait_for(entered.wait(), timeout=5)
            second = asyncio.create_task(runtime.submit(self.event("event-002")))
            await asyncio.sleep(0)
            try:
                with self.assertRaises(RuntimeQueueFullError):
                    await runtime.submit(self.event("event-003"))
            finally:
                release.set()
                await asyncio.gather(first, second)
        self.assertIsNone(await self.storage.get_event("event-003"))

    async def test_caller_cancellation_does_not_undo_accepted_event(self):
        entered, release = asyncio.Event(), asyncio.Event()
        router = EventRouter()

        async def handler(event, state):
            entered.set()
            await release.wait()
            return self.proposal(event, state)

        router.register("ACTIVITY.completed", handler)
        runtime = self.make_runtime(router)
        await runtime.start()
        submitted = asyncio.create_task(runtime.submit(self.event()))
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            submitted.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await submitted
        finally:
            release.set()
            await runtime.stop()
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).version, 1)

    async def test_invalid_runtime_configuration_is_rejected(self):
        for max_pending in (0, -1, True, 1.5):
            with self.subTest(max_pending=max_pending):
                with self.assertRaises(ValueError):
                    self.make_runtime(max_pending=max_pending)
        for agent_id in ("", " ", " role", 123):
            with self.subTest(agent_id=agent_id):
                with self.assertRaises(ValueError):
                    AgentRuntime(agent_id, self.storage, self.router)
        with self.assertRaises(ValueError):
            self.make_runtime(clock=123)

    async def test_start_and_stop_are_repeatable_without_state_reset(self):
        runtime = self.make_runtime()
        await runtime.start()
        await runtime.start()
        await runtime.submit(self.event())
        await runtime.stop()
        await runtime.stop()
        await runtime.start()
        self.assertEqual((await runtime.submit(self.event("event-002"))).version, 2)
        await runtime.stop()

    async def test_handler_cannot_wait_for_its_own_runtime(self):
        router = EventRouter()

        async def handler(event, state):
            with self.assertRaises(AgentRuntimeError):
                await runtime.submit(self.event("event-child"))
            with self.assertRaises(AgentRuntimeError):
                await runtime.stop()
            return self.proposal(event, state)

        router.register("ACTIVITY.completed", handler)
        async with self.make_runtime(router) as runtime:
            self.assertEqual((await runtime.submit(self.event())).version, 1)
        self.assertIsNone(await self.storage.get_event("event-child"))

    async def test_cancelled_stop_wait_does_not_cancel_shutdown(self):
        entered, release = asyncio.Event(), asyncio.Event()
        router = EventRouter()

        async def handler(event, state):
            entered.set()
            await release.wait()
            return self.proposal(event, state)

        router.register("ACTIVITY.completed", handler)
        runtime = self.make_runtime(router)
        await runtime.start()
        submitted = asyncio.create_task(runtime.submit(self.event()))
        await asyncio.wait_for(entered.wait(), timeout=5)
        stopping = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        try:
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
        finally:
            release.set()
            await submitted
            await runtime.stop()
        self.assertFalse(runtime.is_running)
        self.assertEqual((await self.storage.get_state(self.state.agent_id)).version, 1)
