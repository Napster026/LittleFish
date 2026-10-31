"""状态变更提案模型测试，由开发者手动运行。"""

import unittest

from pydantic import ValidationError

from persona_agent.core.settlement import StateChangeProposal
from persona_agent.persona.needs import NeedDelta


class StateChangeProposalTests(unittest.TestCase):
    def proposal_data(self):
        return {
            "proposal_id": "proposal-001",
            "event_id": "event-001",
            "agent_id": "persona-001",
            "expected_version": 0,
            "needs_delta": {"energy": 10, "social_need": -5.5},
        }

    def test_proposal_json_round_trip(self):
        proposal = StateChangeProposal(**self.proposal_data())
        self.assertIsInstance(proposal.needs_delta, NeedDelta)
        self.assertEqual(proposal.needs_delta.energy, 10.0)
        self.assertEqual(proposal.needs_delta.social_need, -5.5)
        self.assertEqual(proposal.needs_delta.satiety, 0.0)
        self.assertEqual(proposal.needs_delta.boredom, 0.0)
        self.assertEqual(proposal.expected_version, 0)
        restored = StateChangeProposal.model_validate_json(proposal.model_dump_json())
        self.assertEqual(restored, proposal)

    def test_signed_deltas_are_not_final_state_values(self):
        # 变化量没有最终状态的 0～100 约束，模型也不静默截断。
        delta = NeedDelta(energy=150, boredom=-150)
        self.assertEqual(delta.energy, 150.0)
        self.assertEqual(delta.boredom, -150.0)
        # 不同需求的变化量即使总和为 0，也不是无变化。
        self.assertEqual(NeedDelta(energy=10, social_need=-10).energy, 10.0)

    def test_invalid_numeric_deltas_are_rejected(self):
        for field in ("energy", "satiety", "boredom", "social_need"):
            for value in (True, False, "10", None, float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, value=value):
                    data = self.proposal_data()
                    data["needs_delta"][field] = value
                    with self.assertRaises(ValidationError):
                        StateChangeProposal(**data)

    def test_zero_delta_is_rejected(self):
        for delta in ({}, {"energy": 0}, {"energy": -0.0}, {
            "energy": 0, "satiety": 0, "boredom": 0, "social_need": 0,
        }):
            with self.subTest(delta=delta):
                data = self.proposal_data()
                data["needs_delta"] = delta
                with self.assertRaises(ValidationError):
                    StateChangeProposal(**data)

    def test_required_fields_cannot_be_omitted(self):
        for field in self.proposal_data():
            with self.subTest(field=field):
                data = self.proposal_data()
                del data[field]
                with self.assertRaises(ValidationError):
                    StateChangeProposal(**data)

    def test_invalid_identifiers_and_version_are_rejected(self):
        for field in ("proposal_id", "event_id", "agent_id"):
            for value in ("", " ", " id", "id ", 123, None):
                with self.subTest(field=field, value=value):
                    data = self.proposal_data()
                    data[field] = value
                    with self.assertRaises(ValidationError):
                        StateChangeProposal(**data)
        for value in (-1, True, "1", 1.0, None):
            with self.subTest(version=value):
                data = self.proposal_data()
                data["expected_version"] = value
                with self.assertRaises(ValidationError):
                    StateChangeProposal(**data)

    def test_unknown_fields_are_rejected(self):
        data = self.proposal_data()
        data["unexpected"] = "value"
        with self.assertRaises(ValidationError):
            StateChangeProposal(**data)
        data = self.proposal_data()
        data["needs_delta"]["unexpected"] = 1
        with self.assertRaises(ValidationError):
            StateChangeProposal(**data)

    def test_proposal_and_nested_delta_are_frozen(self):
        proposal = StateChangeProposal(**self.proposal_data())
        with self.assertRaises(ValidationError):
            proposal.expected_version = 1
        with self.assertRaises(ValidationError):
            proposal.needs_delta.energy = 20
