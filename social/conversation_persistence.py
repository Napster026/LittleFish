"""按角色和会话精确读取续聊游标；事务由应用层提供。"""

from datetime import timedelta

from .conversation import ConversationState, can_continue, ends_conversation
from ..persistence.errors import StorageError
from ..core.scheduler import normalize_schedule_time


class ConversationPersistence:
    def __init__(self, session):
        self.session=session

    @staticmethod
    def record(row):
        item=ConversationState.model_validate_json(row["state_json"])
        if row["agent_id"]!=item.agent_id or row["scope_id"]!=item.scope_id or row["revision"]!=item.revision:
            raise StorageError("会话索引与正文不一致")
        return item

    async def get(self, agent_id, scope_id):
        row=await self.session.fetch_one("conversations",where={"agent_id":agent_id,"scope_id":scope_id})
        return None if row is None else self.record(row)

    async def iter_states(self, agent_id):
        # 只分页读取每个会话的一条游标，不遍历聊天事件历史。
        after=None
        while True:
            rows=await self.session.fetch_all("conversations",where={"agent_id":agent_id},
                after=after,order_by=("scope_id",),limit=100)
            if not rows:
                return
            for row in rows:
                try:
                    item=self.record(row)
                except (StorageError, ValueError):
                    # 单条坏游标不应阻断调度和插件启动；不修改坏数据。
                    continue
                yield item
            after={"scope_id":rows[-1]["scope_id"]}

    async def save(self, item, old):
        row={"agent_id":item.agent_id,"scope_id":item.scope_id,"revision":item.revision,"state_json":item.model_dump_json()}
        if old is None:
            await self.session.insert("conversations",row)
        elif await self.session.update("conversations",row,where={"agent_id":old.agent_id,"scope_id":old.scope_id,"revision":old.revision})!=1:
            raise StorageError("会话版本冲突")

    async def receive(self, agent_id, event, *, now, settings, directed, blocked=False):
        now=normalize_schedule_time(now)
        if event.event_type!="SOCIAL.message_received" or event.provenance.value!="observed" or not event.scope_id:
            raise ValueError("续聊只接受有明确会话的实际入站")
        old=await self.get(agent_id,event.scope_id)
        if not settings.enabled or (settings.private_only and event.payload.get("group_id")) or blocked:
            return False
        if old is not None and (old.source_event_id==event.event_id or event.timestamp<old.updated_at):
            return False
        if not 0<=(now-event.timestamp).total_seconds()<=settings.max_message_age_seconds:
            return False
        follow=can_continue(old,event,now,settings)
        if not directed and not follow:
            return False
        closing=ends_conversation(event.payload.get("text",""),settings)
        item=ConversationState(agent_id=agent_id,scope_id=event.scope_id,
            platform_id=event.payload["platform_id"],user_id=event.payload["sender_id"],
            source_event_id=event.event_id,own_outbound_event_id=None if old is None else old.own_outbound_event_id,
            phase="closed" if closing else "awaiting_reply",
            updated_at=now,expires_at=now+timedelta(seconds=settings.window_seconds),
            followups=0 if directed else old.followups+1,revision=1 if old is None else old.revision+1)
        await self.save(item,old)
        return follow and not directed and not closing

    async def sent(self, agent_id, event, *, now, settings):
        now=normalize_schedule_time(now)
        old=await self.get(agent_id,event.scope_id)
        if (not settings.enabled or old is None or old.source_event_id!=event.event_id
                or old.platform_id!=event.payload.get("platform_id") or old.user_id!=event.payload.get("sender_id")
                or (settings.private_only and event.payload.get("group_id"))
                or old.phase!="awaiting_reply" or now<old.updated_at or now>=old.expires_at):
            return
        item=old.model_copy(update={"phase":"send_hook_observed","updated_at":now,
            "expires_at":now+timedelta(seconds=settings.window_seconds),"revision":old.revision+1})
        await self.save(item,old)
