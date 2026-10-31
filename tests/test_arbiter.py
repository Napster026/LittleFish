"""行为仲裁的硬约束和稳定选择回归，由开发者手动运行。"""

import unittest

from pydantic import ValidationError

from core.arbiter import (
    ActiveBehavior, ArbitrationContext, ArbitrationPolicy, BehaviorArbiter, BehaviorCandidate, BehaviorKind,
)


def candidate(identifier, score=1.0, **kwargs):
    return BehaviorCandidate(
        candidate_id=f"start:{identifier}", kind=BehaviorKind.START_ACTIVITY,
        definition_id=identifier, utility=score, mode=kwargs.pop("mode", "simulated"), **kwargs,
    )


def active(status="running", **kwargs):
    return ActiveBehavior(
        activity_id="existing", definition_id="rest", mode=kwargs.pop("mode", "simulated"),
        status=status, **kwargs,
    )


class ArbiterTests(unittest.TestCase):
    def test_constraints_precede_scores_and_keep_lower_candidate(self):
        arbiter = BehaviorArbiter(ArbitrationPolicy(disabled_definition_ids={"disabled"}))
        result = arbiter.decide(ArbitrationContext(), (
            candidate("disabled", 1000), candidate("real", 1000, mode="real"),
            candidate("unavailable", 1000, unavailable_reason="unknown_duration"),
            candidate("negative", -1), candidate("valid", 0.1),
        ))
        self.assertEqual(result.winner.definition_id, "valid")
        rejected = {a.candidate.definition_id: a.reason for a in result.assessments if not a.allowed}
        self.assertEqual(rejected, {
            "disabled": "activity_disabled", "real": "unsupported_mode",
            "unavailable": "unknown_duration", "negative": "nonpositive_utility",
        })

    def test_running_continues_and_other_occupancy_stays_idle(self):
        arbiter = BehaviorArbiter(ArbitrationPolicy(autonomous_enabled=False))
        result = arbiter.decide(ArbitrationContext(active=(active(mode="real"),)), (candidate("new", 1000),))
        self.assertEqual(result.winner.kind, BehaviorKind.CONTINUE_ACTIVITY)
        self.assertEqual(result.winner.activity_id, "existing")
        for status in ("pending", "paused"):
            result = BehaviorArbiter().decide(
                ArbitrationContext(active=(active(status),)), (candidate("new", 1000),),
            )
            self.assertEqual(result.winner.kind, BehaviorKind.IDLE)
        conflict = ArbitrationContext(active=(active(), ActiveBehavior(
            activity_id="other", definition_id="eat", mode="simulated", status="pending",
        )))
        result = BehaviorArbiter().decide(conflict, (candidate("new", 1000),))
        self.assertEqual(result.reason, "conflicting_activities")

    def test_recovery_requires_origin_matching_target_and_current_permission(self):
        context = ArbitrationContext(active=(active("pending"),), recoverable_activity_ids={"existing"})
        recovery = candidate("rest", 0, activity_id="existing")
        result = BehaviorArbiter().decide(context, (recovery,))
        self.assertEqual(result.reason, "recover_pending")
        self.assertEqual(result.winner, recovery)
        for arbiter, item, reason in (
            (BehaviorArbiter(ArbitrationPolicy(disabled_definition_ids={"rest"})), recovery, "activity_disabled"),
            (BehaviorArbiter(ArbitrationPolicy(autonomous_enabled=False)), recovery, "autonomy_disabled"),
            (BehaviorArbiter(), candidate("other", 1000, activity_id="existing"), "target_mismatch"),
            (BehaviorArbiter(), candidate("rest", 1000, activity_id="unknown"), "not_recoverable"),
        ):
            result = arbiter.decide(context, (item,))
            self.assertEqual(result.winner.kind, BehaviorKind.IDLE)
            self.assertEqual(result.assessments[0].reason, reason)

    def test_ties_idle_and_invalid_inputs(self):
        arbiter = BehaviorArbiter()
        a, b = candidate("a"), candidate("b")
        self.assertEqual(arbiter.decide(ArbitrationContext(), (b, a)).winner, a)
        self.assertEqual(arbiter.decide(ArbitrationContext(), ()).winner.kind, BehaviorKind.IDLE)
        with self.assertRaises(ValueError):
            arbiter.decide(ArbitrationContext(), (a, a))
        for score in (True, "1", float("inf"), float("nan")):
            with self.subTest(score=score), self.assertRaises(ValidationError):
                candidate("bad", score)
        with self.assertRaises(ValidationError):
            ArbitrationPolicy(autonomous_enabled="false")
        with self.assertRaises(ValidationError):
            ArbitrationContext(recoverable_activity_ids={"nonexistent"})
