"""活动定义与实例测试，由开发者手动运行。"""

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from persona_agent.activity.models import (
    ActivityDefinition, ActivityInstance, ActivityStatus, ExecutionMode, create_activity_instance,
)


class ActivityModelTests(unittest.TestCase):
    def definition_data(self):
        return dict(
            definition_id="rest", name="休息", supported_modes=["simulated"],
            estimated_duration_seconds=300, completion_needs_delta={"energy": 10},
        )

    def instance_data(self):
        moment = datetime(2026, 10, 31, 20, 0, tzinfo=timezone(timedelta(hours=8)))
        return dict(
            activity_id="activity-001", definition_id="rest", agent_id="persona-001",
            mode="simulated", created_at=moment, updated_at=moment + timedelta(seconds=10),
            status="running", progress=0.5, stage="resting",
        )

    def test_definition_json_round_trip(self):
        definition = ActivityDefinition(**self.definition_data())
        self.assertEqual(definition.supported_modes, (ExecutionMode.SIMULATED,))
        self.assertEqual(definition.estimated_duration_seconds, 300.0)
        self.assertEqual(definition.completion_needs_delta.energy, 10)
        self.assertEqual(ActivityDefinition.model_validate_json(definition.model_dump_json()), definition)

    def test_duration_effect_and_progress_can_be_unknown(self):
        definition = ActivityDefinition(
            definition_id="watch", name="观看视频", supported_modes=["real", "hybrid"],
        )
        self.assertIsNone(definition.estimated_duration_seconds)
        self.assertIsNone(definition.completion_needs_delta)
        data = self.instance_data()
        data.update(definition_id="watch", mode="real", progress=None)
        instance = ActivityInstance(**data)
        self.assertIsNone(instance.progress)
        self.assertEqual(instance.mode, ExecutionMode.REAL)

    def test_invalid_definition_fields_are_rejected(self):
        invalid_values = {
            "definition_id": ["", " rest", 123],
            "name": ["", "休息 ", None],
            "supported_modes": [[], ["simulated", "simulated"], ["unknown"], "simulated", None],
            "estimated_duration_seconds": [0, -1, True, "300", float("nan"), float("inf")],
            "completion_needs_delta": [{}],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = self.definition_data()
                    data[field] = value
                    with self.assertRaises(ValidationError):
                        ActivityDefinition(**data)

    def test_instance_json_round_trip(self):
        instance = ActivityInstance(**self.instance_data())
        self.assertEqual(instance.status, ActivityStatus.RUNNING)
        self.assertEqual(instance.created_at.hour, 12)
        self.assertEqual(instance.created_at.utcoffset(), timedelta(0))
        self.assertEqual(ActivityInstance.model_validate_json(instance.model_dump_json()), instance)

    def test_invalid_instance_fields_are_rejected(self):
        invalid_values = {
            "activity_id": ["", " id", 123],
            "definition_id": ["", "rest "],
            "agent_id": ["", None],
            "mode": ["unknown", None],
            "status": ["unknown", None],
            "progress": [-0.1, 1.1, True, "0.5", float("nan"), float("inf")],
            "stage": ["", " stage", 123],
            "revision": [-1, True, "1", 1.0],
            "created_at": [datetime(2026, 10, 31), "123", 123],
            "updated_at": [datetime(2026, 10, 31), "2026-10-31T20:00:00", None],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = self.instance_data()
                    data[field] = value
                    with self.assertRaises(ValidationError):
                        ActivityInstance(**data)

    def test_update_time_cannot_precede_creation(self):
        data = self.instance_data()
        data["updated_at"] = data["created_at"] - timedelta(seconds=1)
        with self.assertRaises(ValidationError):
            ActivityInstance(**data)

    def test_status_and_progress_are_consistent(self):
        for status, progress in (("pending", None), ("pending", 0), ("completed", 1), ("running", None)):
            with self.subTest(status=status, progress=progress):
                data = self.instance_data()
                data.update(status=status, progress=progress)
                self.assertEqual(ActivityInstance(**data).progress, progress)
        for status, progress in (("pending", 0.5), ("completed", 0.5), ("completed", None)):
            with self.subTest(status=status, progress=progress):
                data = self.instance_data()
                data.update(status=status, progress=progress)
                with self.assertRaises(ValidationError):
                    ActivityInstance(**data)

    def test_unknown_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            ActivityDefinition(**{**self.definition_data(), "unexpected": 1})
        with self.assertRaises(ValidationError):
            ActivityInstance(**{**self.instance_data(), "unexpected": 1})

    def test_models_and_nested_effect_are_frozen(self):
        definition = ActivityDefinition(**self.definition_data())
        instance = ActivityInstance(**self.instance_data())
        with self.assertRaises(ValidationError):
            definition.name = "changed"
        with self.assertRaises(ValidationError):
            definition.completion_needs_delta.energy = 20
        with self.assertRaises(ValidationError):
            instance.progress = 0.8

    def test_factory_creates_distinct_pending_instances(self):
        definition = ActivityDefinition(**self.definition_data())
        moment = self.instance_data()["created_at"]
        first = create_activity_instance(
            definition, activity_id="activity-001", agent_id="persona-001",
            mode=ExecutionMode.SIMULATED, created_at=moment,
        )
        second = create_activity_instance(
            definition, activity_id="activity-002", agent_id="persona-001",
            mode=ExecutionMode.SIMULATED, created_at=moment,
        )
        self.assertNotEqual(first.activity_id, second.activity_id)
        self.assertEqual(first.definition_id, second.definition_id)
        self.assertEqual(first.status, ActivityStatus.PENDING)
        self.assertEqual(first.progress, 0.0)
        self.assertEqual(first.updated_at, first.created_at)
        real_definition = ActivityDefinition(
            definition_id="watch", name="视频", supported_modes=["real", "hybrid"],
        )
        for mode in (ExecutionMode.REAL, ExecutionMode.HYBRID):
            with self.subTest(mode=mode):
                instance = create_activity_instance(
                    real_definition, activity_id=f"activity-{mode.value}", agent_id="persona-001",
                    mode=mode, created_at=moment,
                )
                self.assertIsNone(instance.progress)

    def test_factory_rejects_unsupported_mode(self):
        definition = ActivityDefinition(**self.definition_data())
        with self.assertRaisesRegex(ValueError, "不支持"):
            create_activity_instance(
                definition, activity_id="activity-001", agent_id="persona-001",
                mode=ExecutionMode.REAL, created_at=self.instance_data()["created_at"],
            )

    def test_required_fields_cannot_be_omitted(self):
        for field in ("definition_id", "name", "supported_modes"):
            data = self.definition_data()
            del data[field]
            with self.subTest(definition_field=field):
                with self.assertRaises(ValidationError):
                    ActivityDefinition(**data)
        for field in ("activity_id", "definition_id", "agent_id", "mode", "created_at", "updated_at"):
            data = self.instance_data()
            del data[field]
            with self.subTest(instance_field=field):
                with self.assertRaises(ValidationError):
                    ActivityInstance(**data)
