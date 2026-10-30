"""统一事件模型：字段校验、来源追溯与 JSON 序列化。"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue, StrictStr, field_validator


class EventCategory(str, Enum):
    """事件的五大类别；具体事件名称由各业务模块定义。"""

    SOCIAL = "SOCIAL"
    ACTIVITY = "ACTIVITY"
    OBSERVATION = "OBSERVATION"
    INTERNAL = "INTERNAL"
    SYSTEM = "SYSTEM"


class EventProvenance(str, Enum):
    """信息如何来的"""

    OBSERVED = "observed"
    SIMULATED = "simulated"
    INFERRED = "inferred"
    CONFIRMED = "confirmed"


def _check_json_value(value: Any, ancestors: set[int]) -> None:
    """拒绝隐式类型转换、非有限数值以及循环引用。"""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("payload 不允许包含 NaN 或 Infinity")
        return
    if type(value) not in (dict, list):
        raise ValueError("payload 只能包含 JSON 对象、数组、字符串、数值、布尔值和 null")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("payload 不允许包含循环引用")
    ancestors.add(identity)
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("payload 中的对象键必须是字符串")
                _check_json_value(item, ancestors)
        else:
            for item in value:
                _check_json_value(item, ancestors)
    finally:
        ancestors.remove(identity)


# noinspection PyNestedDecorators
class AgentEvent(BaseModel):
    """事件信封，使用 ‘类别.小写动作名’ 命名具体事件。

    event_id 由生产方分配，重试必须沿用原 ID。跨事件唯一性与重复结算
    防护由后续存储和结算模块实现。provenance 必须显式提供。
    frozen 只禁止字段重新赋值；payload 内嵌字典与列表仍应由调用方视为只读。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    event_id: StrictStr
    event_type: StrictStr
    timestamp: AwareDatetime
    source: StrictStr
    payload: dict[str, JsonValue]
    provenance: EventProvenance
    scope_id: StrictStr | None = None
    activity_id: StrictStr | None = None
    correlation_id: StrictStr | None = None
    causation_id: StrictStr | None = None
    schema_version: Literal[1] = 1

    @field_validator(
        "event_id", "source", "scope_id", "activity_id", "correlation_id", "causation_id"
    )
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("标识和来源不能为空，也不能包含首尾空白")
        return value

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        category, separator, action = value.partition(".")
        if (
            not separator
            or category not in {item.value for item in EventCategory}
            or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", action) is None
        ):
            raise ValueError("event_type 必须使用已知类别与小写动作名，例如 SOCIAL.message_received")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def reject_numeric_timestamp(cls, value: Any) -> Any:
        if not isinstance(value, (datetime, str)):
            raise ValueError("timestamp 必须是带时区的 datetime 或 ISO 8601 字符串")
        if isinstance(value, str):
            # 先按 ISO 格式解析，防止数值字符串被当作 Unix 时间戳接受。
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> Any:
        if type(value) is not dict:
            raise ValueError("payload 必须是 JSON 对象")
        _check_json_value(value, set())
        return value

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: Any) -> Any:
        if type(value) is not int or value != 1:
            raise ValueError("当前仅支持整数 schema_version=1")
        return value

    @property
    def category(self) -> EventCategory:
        """从事件类型派生类别，不重复写入序列化数据。"""
        return EventCategory(self.event_type.partition(".")[0])
