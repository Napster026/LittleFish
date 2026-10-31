"""角色状态模型测试，由开发者手动运行。"""

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from persona_agent.core.state import AgentState
from persona_agent.persona.needs import NeedState
from persona_agent.persona.emotion import EmotionDynamics, EmotionState, VAD


class AgentStateTests(unittest.TestCase):
    def state_data(self):
        """明确提供示例初始值，这些值不是角色的默认配置。"""
        return {
            "agent_id": "persona-001",
            "needs": {
                "energy": 40,
                "satiety": 70,
                "boredom": 30,
                "social_need": 50,
            },
            "updated_at": datetime(
                2026, 10, 31, 20, 0, tzinfo=timezone(timedelta(hours=8))
            ),
        }

    def test_state_json_round_trip(self):
        state = AgentState(**self.state_data())
        self.assertIsInstance(state.needs, NeedState)
        self.assertEqual(state.version, 0)
        self.assertEqual(state.needs.energy, 40.0)
        self.assertEqual(state.updated_at.hour, 12)
        self.assertEqual(state.updated_at.utcoffset(), timedelta(0))
        self.assertEqual(AgentState.model_validate_json(state.model_dump_json()), state)

        # 已保存状态的版本与更新时间在加载时必须保留。
        data = self.state_data()
        data["version"] = 7
        saved = AgentState(**data)
        self.assertEqual(AgentState.model_validate_json(saved.model_dump_json()), saved)

    def test_optional_emotion_round_trip_and_time_boundary(self):
        state = AgentState(**self.state_data())
        self.assertIsNone(state.emotion)
        zero = VAD(valence=0, arousal=0, dominance=0)
        emotion = EmotionState(
            current=zero, mood=zero, baseline=zero, updated_at=state.updated_at,
            dynamics=EmotionDynamics(emotion_half_life_seconds=300, mood_half_life_seconds=7200,
                                     mood_impulse_ratio=0.1),
        )
        populated = AgentState(**{**state.model_dump(), "emotion": emotion})
        self.assertEqual(AgentState.model_validate_json(populated.model_dump_json()), populated)
        future = EmotionState(**{**emotion.model_dump(), "updated_at": state.updated_at + timedelta(seconds=1)})
        with self.assertRaises(ValidationError):
            AgentState(**{**state.model_dump(), "emotion": future})

    def test_need_boundaries_are_accepted(self):
        for field in self.state_data()["needs"]:
            for value in (0, 100, 50.5):
                with self.subTest(field=field, value=value):
                    data = self.state_data()
                    data["needs"][field] = value
                    state = AgentState(**data)
                    self.assertEqual(getattr(state.needs, field), float(value))

    def test_invalid_needs_are_rejected(self):
        for field in self.state_data()["needs"]:
            for value in (-0.1, 100.1, float("nan"), float("inf"), True, "50", None):
                with self.subTest(field=field, value=value):
                    data = self.state_data()
                    data["needs"][field] = value
                    with self.assertRaises(ValidationError):
                        AgentState(**data)

    def test_required_fields_cannot_be_omitted(self):
        for field in ("agent_id", "needs", "updated_at"):
            with self.subTest(field=field):
                data = self.state_data()
                del data[field]
                with self.assertRaises(ValidationError):
                    AgentState(**data)
        for field in self.state_data()["needs"]:
            with self.subTest(need=field):
                data = self.state_data()
                del data["needs"][field]
                with self.assertRaises(ValidationError):
                    AgentState(**data)

    def test_invalid_metadata_is_rejected(self):
        invalid_values = {
            "agent_id": ("", " ", " persona-001", 123),
            "version": (-1, True, "1", 1.0),
            "updated_at": (
                datetime(2026, 10, 31), "2026-10-31T20:00:00", 123, "123",
            ),
            "needs_updated_at": (
                datetime(2026, 10, 31), 123, "123",
                datetime(2026, 11, 1, tzinfo=timezone.utc),
            ),
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = self.state_data()
                    data[field] = value
                    with self.assertRaises(ValidationError):
                        AgentState(**data)

    def test_unknown_fields_are_rejected(self):
        data = self.state_data()
        data["unexpected"] = "value"
        with self.assertRaises(ValidationError):
            AgentState(**data)
        data = self.state_data()
        data["needs"]["unexpected"] = 50
        with self.assertRaises(ValidationError):
            AgentState(**data)

    def test_state_and_nested_needs_are_frozen(self):
        state = AgentState(**self.state_data())
        with self.assertRaises(ValidationError):
            state.version = 1
        with self.assertRaises(ValidationError):
            state.needs.energy = 100
