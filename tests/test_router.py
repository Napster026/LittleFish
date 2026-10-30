"""路由及路由到存储的衔接测试，由开发者手动运行。"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.events import AgentEvent
from core.router import (
    DuplicateHandlerError,
    EventRouter,
    InvalidHandlerError,
    InvalidHandlerResultError,
)
from core.settlement import StateChangeProposal
from core.state import AgentState
from infrastructure.storage import SQLiteStorage
from persona.needs import NeedState


class EventRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.router = EventRouter()
        self.time = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.event = AgentEvent(
            event_id="event-001", event_type="ACTIVITY.completed", timestamp=self.time,
            source="activity.simulated", provenance="simulated",
            payload={"definition_id": "rest", "details": {"stages": ["resting"]}},
        )
        self.state = AgentState(
            agent_id="persona-001", updated_at=self.time,
            needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
        )

    def proposal(self, **updates):
        data = dict(
            proposal_id="proposal-001", event_id=self.event.event_id,
            agent_id=self.state.agent_id, expected_version=self.state.version,
            needs_delta={"energy": 10},
        )
        data.update(updates)
        return StateChangeProposal(**data)

    async def handle_rest(self, event, state):
        return StateChangeProposal(
            proposal_id="proposal-001", event_id=event.event_id,
            agent_id=state.agent_id, expected_version=state.version,
            needs_delta={"energy": 10},
        )

    async def test_dispatches_matching_event_with_state(self):
        received = []

        async def handler(event, state):
            received.append((event, state))
            return self.proposal()

        self.router.register("ACTIVITY.completed", handler)
        result = await self.router.dispatch(self.event, self.state)
        self.assertEqual(result, self.proposal())
        self.assertEqual(received, [(self.event, self.state)])

    async def test_unregistered_event_returns_none(self):
        self.router.register("ACTIVITY.started", self.handle_rest)
        self.assertIsNone(await self.router.dispatch(self.event, self.state))

    async def test_handler_can_return_none(self):
        async def handler(event, state):
            return None

        self.router.register("ACTIVITY.completed", handler)
        self.assertIsNone(await self.router.dispatch(self.event, self.state))

    async def test_duplicate_registration_does_not_replace_handler(self):
        self.router.register("ACTIVITY.completed", self.handle_rest)

        async def replacement(event, state):
            return None

        with self.assertRaises(DuplicateHandlerError):
            self.router.register("ACTIVITY.completed", replacement)
        self.assertEqual(await self.router.dispatch(self.event, self.state), self.proposal())

    async def test_invalid_event_types_are_rejected(self):
        for event_type in ("ACTIVITY", "UNKNOWN.completed", "ACTIVITY.Completed", "", 123, None):
            with self.subTest(event_type=event_type):
                with self.assertRaises(ValueError):
                    self.router.register(event_type, self.handle_rest)

    async def test_non_callable_handler_is_rejected(self):
        with self.assertRaises(InvalidHandlerError):
            self.router.register("ACTIVITY.completed", None)

    async def test_non_awaitable_handler_is_rejected(self):
        def handler(event, state):
            return self.proposal()

        self.router.register("ACTIVITY.completed", handler)
        with self.assertRaises(InvalidHandlerError):
            await self.router.dispatch(self.event, self.state)

    async def test_invalid_result_type_is_rejected(self):
        for value in ({"energy": 10}, 10, "proposal"):
            with self.subTest(value=value):
                async def handler(event, state):
                    return value

                router = EventRouter()
                router.register("ACTIVITY.completed", handler)
                with self.assertRaises(InvalidHandlerResultError):
                    await router.dispatch(self.event, self.state)

    async def test_wrong_proposal_association_is_rejected(self):
        for updates in (
            {"event_id": "event-other"}, {"agent_id": "persona-other"}, {"expected_version": 1},
        ):
            with self.subTest(updates=updates):
                async def handler(event, state):
                    return self.proposal(**updates)

                router = EventRouter()
                router.register("ACTIVITY.completed", handler)
                with self.assertRaises(InvalidHandlerResultError):
                    await router.dispatch(self.event, self.state)

    async def test_handler_failure_is_not_silently_ignored(self):
        async def handler(event, state):
            raise RuntimeError("handler failure")

        self.router.register("ACTIVITY.completed", handler)
        with self.assertRaisesRegex(RuntimeError, "handler failure"):
            await self.router.dispatch(self.event, self.state)

    async def test_handler_receives_independent_input_data(self):
        before = self.event.model_dump_json()

        async def handler(event, state):
            self.assertIsNot(event, self.event)
            self.assertIsNot(state, self.state)
            event.payload["details"]["stages"].append("changed")
            return None

        self.router.register("ACTIVITY.completed", handler)
        await self.router.dispatch(self.event, self.state)
        self.assertEqual(self.event.model_dump_json(), before)

    async def test_event_can_be_routed_and_committed(self):
        self.router.register("ACTIVITY.completed", self.handle_rest)
        with TemporaryDirectory() as directory:
            async with SQLiteStorage(Path(directory) / "agent.sqlite3") as storage:
                await storage.create_state(self.state)
                await storage.record_event(self.event)
                current = await storage.get_state(self.state.agent_id)
                proposal = await self.router.dispatch(self.event, current)
                self.assertIsInstance(proposal, StateChangeProposal)
                result = await storage.commit_proposal(
                    proposal, settled_at=self.time + timedelta(seconds=1),
                )
                self.assertEqual(result.needs.energy, 50)
                self.assertEqual(result.version, 1)
                self.assertEqual(await storage.get_state(self.state.agent_id), result)
