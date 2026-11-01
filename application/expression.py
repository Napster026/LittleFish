"""主动表达仓库与策略适配；所有读写由外层事务协调。"""

from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5
from ..core.arbiter import BehaviorCandidate, BehaviorKind
from ..core.events import AgentEvent
from ..social.expression import ExpressionHead, ExpressionIntent, gate, local_day
from ..social.conversation_persistence import ConversationPersistence
from ..persistence.errors import StorageError
from ..social.expression_clock import expression_deadline_for


class ExpressionPersistence:
    def __init__(self,core):self.core,self.session=core,core.session

    async def get(self,agent_id):
        row=await self.session.fetch_one("expression_heads",where={"agent_id":agent_id})
        if row is None:return None
        head=ExpressionHead.model_validate_json(row["state_json"])
        if head.agent_id!=agent_id or head.revision!=row["revision"] or head.intent.agent_id!=agent_id:
            raise StorageError("主动表达游标与正文不一致")
        return head

    async def save(self,head,old):
        values={"agent_id":head.agent_id,"revision":head.revision,"state_json":head.model_dump_json()}
        if old is None:await self.session.insert("expression_heads",values)
        elif await self.session.update("expression_heads",values,where={"agent_id":head.agent_id,"revision":old.revision})!=1:
            raise StorageError("主动表达版本冲突")

    async def target(self,agent_id,settings,now,allowed):
        found=[]
        for target in settings.targets:
            if allowed is not None and not allowed(target):continue
            state=await ConversationPersistence(self.session).get(agent_id,target.scope_id)
            if state is None or state.user_id!=target.user_id or state.platform_id!=target.platform_id:continue
            event=await self.core.get_event(state.source_event_id)
            if (event is None or event.event_type!="SOCIAL.message_received" or event.provenance.value!="observed" or event.source!="astrbot.aiocqhttp"
                    or event.scope_id!=target.scope_id or event.payload.get("group_id")
                    or event.payload.get("sender_id")!=target.user_id or event.payload.get("platform_id")!=target.platform_id
                    or event.payload.get("bot_id")==target.user_id):continue
            age=(now-event.timestamp).total_seconds()
            if settings.min_silence_seconds<=age<=settings.max_interaction_age_seconds:
                found.append((event.timestamp,target.scope_id,target,event.event_id))
        if not found:return None
        chosen=sorted(found,key=lambda row:(-row[0].timestamp(),row[1]))[0]
        return chosen[2],chosen[3]

    async def candidate(self,agent_id,state,settings,now,allowed,*,occupied=False,active=False):
        if settings is None or not settings.targets or occupied or active:return None
        if state.needs.social_need<settings.social_threshold:return None
        head=await self.get(agent_id)
        if gate(settings,now,head) is not None:return None
        target=await self.target(agent_id,settings,now,allowed)
        if target is None:return None
        return BehaviorCandidate(candidate_id="express:"+target[0].scope_id,kind=BehaviorKind.EXPRESS,
            target_scope_id=target[0].scope_id,utility=state.needs.social_need/100),target

    async def reserve(self,agent_id,key,target,settings,now):
        old=await self.get(agent_id)
        identifier="expression:"+str(uuid5(NAMESPACE_URL,agent_id+":"+key))
        intent=ExpressionIntent(intent_id=identifier,agent_id=agent_id,target=target[0],source_event_id=target[1],
            created_at=now,expires_at=now+timedelta(seconds=settings.intent_ttl_seconds),updated_at=now,
            mode="send" if settings.send_enabled else "preview",status="pending" if settings.send_enabled else "preview")
        head=ExpressionHead(agent_id=agent_id,revision=1 if old is None else old.revision+1,
            budget_day=local_day(now,settings),used=(old.used if old is not None and old.budget_day==local_day(now,settings) else 0)+1,
            last_attempt_at=now,intent=intent)
        await self.save(head,old)
        await self.audit(intent)
        if intent.mode=="send":
            await self.core.create_schedule(expression_deadline_for(intent))
        return intent

    async def audit(self,intent):
        await self.core.record_event(AgentEvent(event_id=intent.intent_id+":"+intent.status,
            event_type="INTERNAL.expression_updated",timestamp=intent.updated_at,source="application.expression",
            provenance="inferred",scope_id=intent.target.scope_id,causation_id=intent.source_event_id,
            payload={"agent_id":intent.agent_id,"intent":intent.model_dump(mode="json")}))

    async def transition(self,head,status,now,*,body=None,reason=None):
        intent=head.intent.model_copy(update={"status":status,"updated_at":now,
            "body":head.intent.body if body is None else body,"reason":reason or head.intent.reason})
        changed=ExpressionHead(**{**head.model_dump(),"revision":head.revision+1,"intent":intent})
        await self.save(changed,head);await self.audit(intent)
        return intent

    async def cancel_inbound(self,agent_id,event,now):
        head=await self.get(agent_id)
        if (head is not None and head.intent.target.scope_id==event.scope_id
                and head.intent.source_event_id!=event.event_id and head.intent.status in ("pending","generating")):
            await self.transition(head,"cancelled",now,reason="new_inbound")
