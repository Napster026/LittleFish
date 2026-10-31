"""事件模型测试，由开发者手动运行。"""

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from persona_agent.core.events import AgentEvent, EventCategory, EventProvenance


class AgentEventTests(unittest.TestCase):
    def event_data(self):
        return {
            "event_id": "event-social-001",
            "event_type": "SOCIAL.message_received",
            "timestamp": datetime(2026, 10, 30, 20, 0, tzinfo=timezone(timedelta(hours=8))),
            "source": "astrbot",
            "payload": {"message_id": "message-001", "text": "你好", "mentions": []},
            "scope_id": "group-001",
            "provenance": "observed",
        }

    def test_social_event_json_round_trip(self):
        event = AgentEvent(**self.event_data())
        self.assertEqual(event.category, EventCategory.SOCIAL)
        self.assertEqual(event.provenance, EventProvenance.OBSERVED)
        self.assertEqual(event.timestamp.hour, 12)
        self.assertEqual(event.timestamp.utcoffset(), timedelta(0))
        restored = AgentEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(restored, event)

    def test_simulated_activity_json_round_trip(self):
        data = self.event_data()
        data.update(
            event_id="event-activity-001", event_type="ACTIVITY.started",
            source="activity.simulated", activity_id="activity-001",
            provenance="simulated", payload={"definition_id": "rest", "progress": 0.0},
        )
        event = AgentEvent(**data)
        self.assertEqual(event.category, EventCategory.ACTIVITY)
        self.assertEqual(event.provenance, EventProvenance.SIMULATED)
        self.assertEqual(AgentEvent.model_validate_json(event.model_dump_json()), event)

    def test_invalid_fields_are_rejected(self):
        invalid_values = {
            "event_id": ["", "  ", " id", 123],
            "source": ["", "astrbot "],
            "event_type": ["UNKNOWN.started", "SOCIAL", "SOCIAL.MessageReceived"],
            "timestamp": [
                datetime(2026, 10, 30), "2026-10-30T20:00:00", 1793361600, "1793361600",
            ],
            "provenance": ["unknown", None],
            "scope_id": [""], "activity_id": [""],
            "correlation_id": [""], "causation_id": [""],
            "schema_version": [0, 2, True, "1", 1.0],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = self.event_data()
                    data[field] = value
                    with self.assertRaises(ValidationError):
                        AgentEvent(**data)

    def test_provenance_is_required(self):
        data = self.event_data()
        del data["provenance"]
        with self.assertRaises(ValidationError):
            AgentEvent(**data)

    def test_unknown_fields_are_rejected(self):
        data = self.event_data()
        data["unexpected"] = "value"
        with self.assertRaises(ValidationError):
            AgentEvent(**data)

    def test_non_json_payload_is_rejected(self):
        cyclic = {}
        cyclic["self"] = cyclic
        invalid_payloads = [
            [], {"number": float("nan")}, {"nested": [float("inf")]},
            {1: "not a string key"}, {"time": datetime.now(timezone.utc)},
            {"values": (1, 2)}, cyclic,
        ]
        for payload in invalid_payloads:
            with self.subTest(payload_type=type(payload).__name__):
                data = self.event_data()
                data["payload"] = payload
                with self.assertRaises(ValidationError):
                    AgentEvent(**data)


if __name__ == "__main__":
    unittest.main()
