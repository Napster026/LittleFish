"""聊天上下文的事务只读视图、反馈索引和请求装配；不依赖 AstrBot SDK。"""

from datetime import timedelta, timezone
from ..activity.feedback import parse_activity_feedback
from ..memory.persistence import MemoryPersistence
from ..cognition.prompt_builder import ChatSnapshot, ContextActivity, ContextFeedback, ContextMemory
from ..core.scheduler import normalize_schedule_time
from ..persistence.errors import StorageError, StateNotFoundError
from ..persona.emotion import EmotionEngine
from ..persona.needs import NeedEngine, NeedState


async def record_feedback_head(core, event):
    if event.scope_id is None:
        return  # 无会话归属不推定为公开，旧反馈不自动共享到所有聊天。
    agent = event.payload.get("agent_id")
    current = await core.session.fetch_one("feedback_heads", where={"agent_id": agent, "scope_id": event.scope_id})
    stamp = event.timestamp.isoformat(timespec="microseconds")
    if current is not None and (current["occurred_at"], current["event_id"]) >= (stamp, event.event_id):
        return
    values = {"event_id": event.event_id, "occurred_at": stamp}
    if current is None:
        await core.session.insert("feedback_heads", {"agent_id": agent, "scope_id": event.scope_id, **values})
    else:
        changed = await core.session.update("feedback_heads", values,
            where={"agent_id": agent, "scope_id": event.scope_id, "event_id": current["event_id"]})
        if changed != 1:
            raise StorageError("反馈指针已经变化")


async def read_chat_snapshot(core, activities, agent_id, scope_id, *, now, rates, labels, include_feedback,
                             memory_settings=None, query="", speaker_id=None, include_memories=True):
    now = normalize_schedule_time(now)
    state = await core.get_state(agent_id)
    if state is None:
        raise StateNotFoundError("角色不存在")
    if now < state.updated_at:
        raise ValueError("读取时间不能早于已提交状态")
    needs = state.needs
    advanced = NeedEngine(rates).advance(needs, from_at=state.needs_updated_at, to_at=now)
    if advanced is not None and advanced.delta is not None:
        needs = NeedState(**{name: max(0, min(100, getattr(needs, name)+getattr(advanced.delta, name)))
                            for name in ("energy", "satiety", "boredom", "social_need")})
    emotion = state.emotion
    if emotion is not None:
        projected = EmotionEngine().advance(emotion, to_at=now)
        emotion = emotion if projected is None else projected.state
    active = await activities.list_active_activities(agent_id)
    priority = {"running": 0, "paused": 1, "pending": 2}
    active = sorted(active, key=lambda record: (priority[record.instance.status.value], record.instance.activity_id))
    views = tuple(ContextActivity(label=labels.get(record.definition.definition_id, "自定义活动"),
        mode=record.instance.mode.value, status=record.instance.status.value, progress=record.instance.progress)
        for record in active)
    feedback = None
    if include_feedback:
        head = await core.session.fetch_one("feedback_heads", where={"agent_id": agent_id, "scope_id": scope_id})
        if head is not None:
            event = await core.get_event(head["event_id"])
            if (event is None or event.scope_id != scope_id or event.payload.get("agent_id") != agent_id
                    or event.timestamp.isoformat(timespec="microseconds") != head["occurred_at"]):
                raise StorageError("反馈指针与原事件不一致")
            record = await activities.get_activity(agent_id, event.activity_id)
            if record is None:
                raise StorageError("反馈所属活动不存在")
            item = parse_activity_feedback(record, event)
            if item is None:
                raise StorageError("反馈指针未引用内容反馈")
            feedback = ContextFeedback(scope_id=scope_id, source_event_id=event.event_id, summary=item.summary,
                                       provenance=event.provenance.value, occurred_at=event.timestamp)
    memories=()
    if memory_settings is not None and include_memories and memory_settings.enabled:
        found=await MemoryPersistence(core).recall(agent_id,scope_id,memory_settings,query=query,speaker_id=speaker_id,now=now)
        zone=timezone(timedelta(minutes=memory_settings.utc_offset_minutes))
        memories=tuple(ContextMemory(memory_id=item.memory_id,scope_id=item.scope_id,
            source_event_id=item.source_event_id,about_id=item.about_id,claim_type=item.claim_type,
            text=item.text,provenance=item.provenance.value,occurred_at=item.occurred_at.astimezone(zone),
            target=None if item.target_start_at is None else (
                str(item.target_start_at.astimezone(zone).date()) if item.time_precision=="day" else
                item.target_start_at.astimezone(zone).isoformat()+"/"+item.target_end_at.astimezone(zone).isoformat()),
            plan_status=item.plan_status) for item in found)
    return ChatSnapshot(agent_id=agent_id, scope_id=scope_id, state_version=state.version, as_of=now,
                        needs=needs, emotion=None if emotion is None else emotion.current,
                        activities=views, feedback=feedback, memories=memories)


class _ContextPart(dict):
    """ProviderRequest 接受普通文本 dict；所有权元数据是属性，不进入请求 JSON。"""
    def __init__(self, text, guard_suffix):
        super().__init__(type="text", text=text)
        self.guard_suffix = guard_suffix


def apply_prompt_context(request, context):
    """只刷新本插件插入的数据块；固定系统约束不随状态变化破坏提示前缀。"""
    system = request.system_prompt
    parts = request.extra_user_content_parts
    if not isinstance(system, str) or not isinstance(parts, list):
        raise TypeError("ProviderRequest 的提示或附加文本类型不正确")
    others = []
    for part in parts:
        if not isinstance(part, _ContextPart):
            others.append(part)
            continue
        if part.guard_suffix:
            start = system.rfind(part.guard_suffix)
            if start >= 0:
                system = system[:start]+system[start+len(part.guard_suffix):]
    if context is not None:
        suffix = ""
        if context.system_text not in system:
            suffix = "\n\n"+context.system_text
            system += suffix
        others.append(_ContextPart(context.user_text, suffix))
    request.system_prompt = system
    request.extra_user_content_parts = others
