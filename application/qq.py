"""QQ 入站边界：使用 AstrBot 公开的统一标识，不导入平台 SDK。"""

import json
import math
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from ..core.events import AgentEvent, EventProvenance
from ..core.scheduler import normalize_schedule_time
from ..core.state import AgentState


QQ_ADAPTERS = frozenset({"aiocqhttp", "qq_official", "qq_official_webhook"})


def is_qq_event(event) -> bool:
    return event.get_platform_name() in QQ_ADAPTERS


def qq_scope(event) -> dict[str, str]:
    def identifier(value):
        return str(value) if type(value) in (str, int) else ""
    return {"platform": identifier(event.get_platform_name()), "platform_id": identifier(event.get_platform_id()),
            "scope_id": identifier(event.unified_msg_origin), "group_id": identifier(event.get_group_id()),
            "sender_id": identifier(event.get_sender_id()), "bot_id": identifier(event.get_self_id())}


def adapt_qq_message(event) -> AgentEvent:
    scope = qq_scope(event)
    if scope["platform"] not in QQ_ADAPTERS:
        raise ValueError("不是当前支持的 QQ 平台")
    message_id = getattr(event.message_obj, "message_id", None)
    if type(message_id) not in (str, int) or str(message_id).strip() == "":
        raise ValueError("QQ 消息缺少稳定消息 ID")
    if not all(scope[key].strip() for key in ("platform_id", "scope_id", "sender_id", "bot_id")):
        raise ValueError("QQ 消息缺少平台、会话、发送者或机器人标识")
    timestamp = getattr(event.message_obj, "timestamp", None)
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
        raise ValueError("QQ 消息缺少有效原始时间")
    time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    identity = json.dumps([scope["platform_id"], scope["bot_id"], scope["scope_id"], str(message_id)],
                          ensure_ascii=False, separators=(",", ":"))
    text = event.get_message_str()
    if not isinstance(text, str):
        raise ValueError("消息文本必须是字符串")
    return AgentEvent(
        event_id=f"qq:{uuid5(NAMESPACE_URL, identity)}", event_type="SOCIAL.message_received",
        timestamp=time, source=f"astrbot.{scope['platform']}", provenance=EventProvenance.OBSERVED,
        scope_id=scope["scope_id"], payload={**scope, "message_id": str(message_id),
                                           "text": text[:8000], "text_truncated": len(text) > 8000},
    )


def message_time_adjustment_id(agent_id: str, event_id: str) -> str:
    AgentState.validate_agent_id(agent_id)
    identity = json.dumps([agent_id, event_id], ensure_ascii=False, separators=(",", ":"))
    return f"qq-time-adjustment:{uuid5(NAMESPACE_URL, identity)}"


def build_message_time_adjustment(agent_id: str, event: AgentEvent, *, platform_timestamp: datetime,
                                  received_at: datetime) -> AgentEvent:
    """钳制审计另存为关联事件，不改变 QQ 消息 payload 与已有消息去重规则。"""
    platform_timestamp = normalize_schedule_time(platform_timestamp)
    received_at = normalize_schedule_time(received_at)
    if (event.event_type != "SOCIAL.message_received" or event.provenance != EventProvenance.OBSERVED
            or event.scope_id is None or event.timestamp != received_at or platform_timestamp <= received_at):
        raise ValueError("时间调整记录要求原始平台时间在未来，且消息时间已钳制到接收时刻")
    return AgentEvent(event_id=message_time_adjustment_id(agent_id, event.event_id),
        event_type="INTERNAL.message_time_adjusted", timestamp=received_at, source="application.qq",
        provenance=EventProvenance.OBSERVED, scope_id=event.scope_id, causation_id=event.event_id,
        payload={"agent_id": agent_id, "platform_timestamp": platform_timestamp.isoformat(),
                 "received_at": received_at.isoformat(),
                 "offset_seconds": (platform_timestamp-received_at).total_seconds()})
