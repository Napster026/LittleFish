"""内存结算测试，由开发者手动运行。"""

import unittest
from datetime import datetime, timedelta, timezone

from core.settlement import (
    AgentMismatchError,
    SettlementTimeError,
    StateChangeProposal,
    StateVersionConflictError,
    settle_state,
)
from core.state import AgentState
from persona.needs import NeedDelta, NeedState


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.state = AgentState(
            agent_id="persona-001",
            needs=NeedState(energy=40, satiety=70, boredom=30, social_need=50),
            updated_at=datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc),
            version=3,
        )
        self.proposal = StateChangeProposal(
            proposal_id="proposal-001", event_id="event-001", agent_id="persona-001",
            expected_version=3, needs_delta=NeedDelta(energy=10, social_need=-5),
        )
        self.settled_at = self.state.updated_at + timedelta(seconds=1)

    def make_proposal(self, **updates):
        data = self.proposal.model_dump()
        data.update(updates)
        return StateChangeProposal(**data)

    def test_successful_settlement(self):
        result = settle_state(self.state, self.proposal, settled_at=self.settled_at)
        self.assertEqual(result.agent_id, self.state.agent_id)
        self.assertEqual(result.needs.energy, 50.0)
        self.assertEqual(result.needs.social_need, 45.0)
        self.assertEqual(result.needs.satiety, 70.0)
        self.assertEqual(result.needs.boredom, 30.0)
        self.assertEqual(result.version, 4)
        self.assertEqual(result.updated_at, self.settled_at)

    def test_need_values_are_clamped(self):
        for name in ("energy", "satiety", "boredom", "social_need"):
            for delta, expected in ((150, 100.0), (-150, 0.0)):
                with self.subTest(field=name, delta=delta):
                    proposal = self.make_proposal(needs_delta={name: delta})
                    result = settle_state(self.state, proposal, settled_at=self.settled_at)
                    self.assertEqual(getattr(result.needs, name), expected)
                    for other in ("energy", "satiety", "boredom", "social_need"):
                        if other != name:
                            self.assertEqual(
                                getattr(result.needs, other), getattr(self.state.needs, other)
                            )

    def test_inputs_are_not_modified(self):
        state_before = self.state.model_dump_json()
        proposal_before = self.proposal.model_dump_json()
        result = settle_state(self.state, self.proposal, settled_at=self.settled_at)
        self.assertIsNot(result, self.state)
        self.assertIsNot(result.needs, self.state.needs)
        self.assertEqual(self.state.model_dump_json(), state_before)
        self.assertEqual(self.proposal.model_dump_json(), proposal_before)

    def test_wrong_agent_is_rejected(self):
        proposal = self.make_proposal(agent_id="persona-002")
        before = self.state.model_dump_json()
        with self.assertRaises(AgentMismatchError):
            settle_state(self.state, proposal, settled_at=self.settled_at)
        self.assertEqual(self.state.model_dump_json(), before)

    def test_stale_and_future_versions_are_rejected(self):
        for version in (2, 4):
            with self.subTest(version=version):
                proposal = self.make_proposal(expected_version=version)
                with self.assertRaises(StateVersionConflictError):
                    settle_state(self.state, proposal, settled_at=self.settled_at)
        self.assertEqual(self.state.version, 3)
        self.assertEqual(self.state.needs.energy, 40.0)

    def test_settlement_time_is_normalized_to_utc(self):
        local_time = datetime(
            2026, 10, 31, 20, 0, 1, tzinfo=timezone(timedelta(hours=8))
        )
        result = settle_state(self.state, self.proposal, settled_at=local_time)
        self.assertEqual(result.updated_at, self.settled_at)
        self.assertEqual(result.updated_at.utcoffset(), timedelta(0))

    def test_invalid_settlement_time_is_rejected(self):
        for value in (datetime(2026, 10, 31, 12, 0, 1), "2026-10-31T12:00:01Z", 123, None):
            with self.subTest(value=value):
                with self.assertRaises(SettlementTimeError):
                    settle_state(self.state, self.proposal, settled_at=value)

    def test_time_cannot_move_backwards(self):
        with self.assertRaises(SettlementTimeError):
            settle_state(
                self.state, self.proposal,
                settled_at=self.state.updated_at - timedelta(microseconds=1),
            )
        self.assertEqual(self.state.version, 3)

    def test_equal_timestamp_is_accepted(self):
        result = settle_state(self.state, self.proposal, settled_at=self.state.updated_at)
        self.assertEqual(result.updated_at, self.state.updated_at)
        self.assertEqual(result.version, 4)

    def test_saturated_need_still_advances_version(self):
        state = AgentState(
            agent_id=self.state.agent_id,
            needs=NeedState(energy=100, satiety=70, boredom=30, social_need=50),
            updated_at=self.state.updated_at, version=3,
        )
        proposal = self.make_proposal(needs_delta={"energy": 10})
        result = settle_state(state, proposal, settled_at=self.settled_at)
        self.assertEqual(result.needs, state.needs)
        self.assertEqual(result.version, 4)
        self.assertEqual(result.updated_at, self.settled_at)

    def test_sequential_settlement_and_old_version_rejection(self):
        first = settle_state(self.state, self.proposal, settled_at=self.settled_at)
        with self.assertRaises(StateVersionConflictError):
            settle_state(first, self.proposal, settled_at=self.settled_at)
        second_proposal = self.make_proposal(
            proposal_id="proposal-002", event_id="event-002", expected_version=4,
        )
        second = settle_state(first, second_proposal, settled_at=self.settled_at)
        self.assertEqual(second.version, 5)
        self.assertEqual(second.needs.energy, 60.0)
        self.assertEqual(second.needs.social_need, 40.0)
