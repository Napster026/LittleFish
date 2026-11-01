"""关系事务规则；只读写自己的表，不提交角色需求或情绪提案。"""

from .models import RelationshipProfile, RelationshipChange
from .relationships import advance_relationship, regress_relationship
from .clock import build_relationship_day, RELATIONSHIP_SOURCE
from uuid import NAMESPACE_URL, uuid5
import json
import logging
from ..core.events import AgentEvent, EventProvenance
from ..core.scheduler import normalize_schedule_time
from ..persistence.errors import StorageError, StorageNotOpenError


logger=logging.getLogger(__name__)
REGRESSION_BATCH_SIZE=100


class _RegressionRowFailure(Exception):
    pass


def stamp(value):
    return value.isoformat(timespec="microseconds")


def identity(platform_id,user_id):
    if any(not isinstance(v,str) or not v.strip() or v!=v.strip() for v in (platform_id,user_id)):
        raise ValueError("关系查询需要明确的平台及用户身份")
    return platform_id,user_id


class RelationshipPersistence:
    def __init__(self,core):
        self.core,self.session=core,core.session

    @staticmethod
    def row(profile):
        return {"agent_id":profile.agent_id,"platform_id":profile.platform_id,"user_id":profile.user_id,
            "revision":profile.revision,"updated_at":stamp(profile.updated_at),"profile_json":profile.model_dump_json()}

    def profile(self,row):
        profile=RelationshipProfile.model_validate_json(row["profile_json"])
        if any(row.get(k)!=v for k,v in self.row(profile).items() if k!="profile_json"):
            raise StorageError("关系索引与档案不一致")
        return profile

    async def get(self,agent_id,platform_id,user_id):
        identity(platform_id,user_id)
        row=await self.session.fetch_one("relationships",where={"agent_id":agent_id,"platform_id":platform_id,"user_id":user_id})
        return None if row is None else self.profile(row)

    def change(self,row):
        item=RelationshipChange.model_validate_json(row["receipt_json"])
        expected={"agent_id":item.agent_id,"source_event_id":item.source_event_id,"scope_id":item.scope_id,
                  "platform_id":item.platform_id,"user_id":item.user_id,"sort_key":stamp(item.at)+":"+item.source_event_id}
        if any(row.get(k)!=v for k,v in expected.items()):
            raise StorageError("关系回执索引与正文不一致")
        return item

    async def history(self,agent_id,platform_id,user_id,scope_id,*,limit=10):
        identity(platform_id,user_id)
        if not isinstance(scope_id,str) or not scope_id.strip() or type(limit) is not int or not 1<=limit<=100:
            raise ValueError("关系历史需要明确会话范围，数量为 1..100")
        items=[]
        # 互动回执只读原 scope；日回归是当前用户关系本身的数值维护，不含私聊内容。
        for scope in (scope_id,""):
            rows=await self.session.fetch_all("relationship_receipts",where={"agent_id":agent_id,
                "platform_id":platform_id,"user_id":user_id,"scope_id":scope},
                order_by=("sort_key",),descending=True,limit=limit)
            for row in rows:
                item=self.change(row)
                if scope=="" and item.kind!="regression":
                    raise StorageError("无范围关系回执必须是日回归")
                items.append(item)
        items.sort(key=lambda item:(item.at,item.kind=="interaction",item.source_event_id),reverse=True)
        return tuple(items[:limit])

    async def regress_profile(self,profile,settings,*,now,cause=None):
        after,from_day,days=regress_relationship(profile,now=now,settings=settings)
        if after==profile:
            return profile
        changed=await self.session.update("relationships",self.row(after),where={"agent_id":profile.agent_id,
            "platform_id":profile.platform_id,"user_id":profile.user_id,"revision":profile.revision})
        if changed!=1:
            raise StorageError("关系日回归修订冲突")
        if after.affinity==profile.affinity:
            return after  # 仅推进日期，不制造空情绪/需求结算或逐用户审计事件。
        if cause is None:
            cause=build_relationship_day(profile.agent_id,after.affinity_day,settings.utc_offset_minutes,settings.affinity_settlement_hour).event
        await self.core.record_event(cause)
        key=json.dumps([profile.agent_id,profile.platform_id,profile.user_id,after.affinity_day],
                       ensure_ascii=False,separators=(",",":"))
        identifier="relationship-regression:"+str(uuid5(NAMESPACE_URL,key))
        event=AgentEvent(event_id=identifier,event_type="INTERNAL.relationship_regressed",timestamp=now,
            source=RELATIONSHIP_SOURCE,provenance="inferred",causation_id=cause.event_id,
            payload={"agent_id":profile.agent_id,"platform_id":profile.platform_id,"user_id":profile.user_id,
                "from_day":from_day,"to_day":after.affinity_day,"days":days,
                "before":profile.affinity,"after":after.affinity,
                "policy":{"settlement_hour":settings.affinity_settlement_hour,"retention":settings.affinity_daily_retention,"negative_recovery":settings.affinity_negative_recovery,
                          "positive_baseline":settings.affinity_positive_baseline}})
        await self.core.record_event(event)
        item=RelationshipChange(agent_id=profile.agent_id,source_event_id=identifier,scope_id="",
            platform_id=profile.platform_id,user_id=profile.user_id,occurred_at=now,at=now,reason="daily_regression",
            kind="regression",from_day=from_day,to_day=after.affinity_day,days=days,
            familiarity_before=profile.familiarity,familiarity_after=after.familiarity,
            affinity_before=profile.affinity,affinity_after=after.affinity,profile_revision=after.revision)
        await self.session.insert("relationship_receipts",{"agent_id":item.agent_id,"source_event_id":identifier,
            "scope_id":"","platform_id":item.platform_id,"user_id":item.user_id,
            "sort_key":stamp(now)+":"+identifier,"receipt_json":item.model_dump_json()})
        return after

    async def regress_all(self,agent_id,settings,*,now,cause):
        report={"scanned":0,"updated":0,"changed":0,"skipped":0}
        if not settings.enabled or not settings.affinity_regression_enabled:
            return report
        # 使用复合主键的两级游标，避免把多个 after 条件误当成字典序。
        platform_cursor=None
        while True:
            platforms=await self.session.fetch_all("relationships",where={"agent_id":agent_id},
                after=None if platform_cursor is None else {"platform_id":platform_cursor},
                order_by=("platform_id",),limit=1)
            if not platforms:
                break
            platform=platforms[0]["platform_id"]
            user_cursor=None
            while True:
                rows=await self.session.fetch_all("relationships",where={"agent_id":agent_id,"platform_id":platform},
                    after=None if user_cursor is None else {"user_id":user_cursor},order_by=("user_id",),limit=REGRESSION_BATCH_SIZE)
                if not rows:
                    break
                for row in rows:
                    report["scanned"]+=1
                    try:
                        async with self.session.savepoint():
                            try:
                                before=self.profile(row)
                                after=await self.regress_profile(before,settings,now=now,cause=cause)
                            except StorageNotOpenError:
                                raise
                            except (StorageError,ValueError) as exc:
                                raise _RegressionRowFailure(type(exc).__name__) from exc
                    except _RegressionRowFailure as exc:
                        report["skipped"]+=1
                        if report["skipped"]<=5:
                            logger.warning("关系日回归跳过异常档案 error_type=%s；保留原数据，后续重试",str(exc))
                        continue
                    report["updated"]+=int(after!=before)
                    report["changed"]+=int(after.affinity!=before.affinity)
                user_cursor=rows[-1]["user_id"]
            platform_cursor=platform
        if report["skipped"]:
            logger.warning("关系日回归完成：跳过 %d 条异常档案",report["skipped"])
        return report

    async def observe(self,agent_id,source,signal,settings,*,now,directed=False,allow_capture=True):
        now=normalize_schedule_time(now)
        if source.event_type!="SOCIAL.message_received" or source.provenance!=EventProvenance.OBSERVED or source.scope_id is None:
            raise ValueError("关系只接受有会话范围的实际入站消息")
        previous=await self.session.fetch_one("relationship_receipts",where={"agent_id":agent_id,"source_event_id":source.event_id})
        if previous is not None:
            return self.change(previous)
        if await self.core.get_state(agent_id) is None or source.timestamp>now:
            raise ValueError("角色不存在或关系处理时间早于来源")
        platform=source.payload.get("platform_id")
        user=source.payload.get("sender_id")
        valid=lambda v:isinstance(v,str) and bool(v.strip()) and v==v.strip()
        platform=platform if valid(platform) else None
        user=user if valid(user) else None
        group=source.payload.get("group_id")
        is_group=bool(group) or ":GroupMessage:" in source.scope_id
        text=source.payload.get("text")
        profile=after=None
        if not allow_capture:reason="history_not_backfilled"
        elif not settings.enabled:reason="disabled"
        elif platform is None or user is None or not isinstance(group,str):reason="unknown_identity"
        elif user==source.payload.get("bot_id"):reason="self_message"
        elif settings.private_only and is_group:reason="private_only"
        elif is_group and not directed:reason="not_directed"
        elif not isinstance(text,str) or not text.strip():reason="empty_message"
        elif text.lstrip().startswith("/"):reason="command"
        elif (now-source.timestamp).total_seconds()>settings.max_effect_age_seconds:reason="expired"
        else:
            profile=await self.get(agent_id,platform,user)
            if profile is not None and now>=profile.updated_at and source.timestamp>=profile.last_interaction_at:
                profile=await self.regress_profile(profile,settings,now=now)
            after,reason=advance_relationship(profile,agent_id=agent_id,platform_id=platform,user_id=user,
                occurred_at=source.timestamp,now=now,signal=signal,settings=settings)
            if after!=profile:
                if profile is None:
                    await self.session.insert("relationships",self.row(after))
                else:
                    changed=await self.session.update("relationships",self.row(after),where={"agent_id":agent_id,
                        "platform_id":platform,"user_id":user,"revision":profile.revision})
                    if changed!=1:
                        raise StorageError("关系档案修订冲突")
        item=RelationshipChange(agent_id=agent_id,source_event_id=source.event_id,scope_id=source.scope_id,
            platform_id=platform,user_id=user,occurred_at=source.timestamp,at=now,rule_id=signal.rule_id,
            polarity=signal.polarity,reason=reason,familiarity_before=0 if profile is None else profile.familiarity,
            familiarity_after=0 if after is None else after.familiarity,affinity_before=0 if profile is None else profile.affinity,
            affinity_after=0 if after is None else after.affinity,profile_revision=None if after is None else after.revision)
        await self.session.insert("relationship_receipts",{"agent_id":agent_id,"source_event_id":source.event_id,
            "scope_id":source.scope_id,"platform_id":platform,"user_id":user,
            "sort_key":stamp(now)+":"+source.event_id,"receipt_json":item.model_dump_json()})
        return item
