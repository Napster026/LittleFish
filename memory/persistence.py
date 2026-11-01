"""事务内的会话记忆规则；不执行 SQL，不重新结算角色效果。"""

import logging
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5
from .models import MemoryDraft, MemoryRecord, MemoryWriteResult, MemoryIndexRepairReport, ActivityMemoryContext
from .extraction import actor_id, extract_memory, target_range
from .retrieval import (INDEX_VERSION, QUERY_TERM_LIMIT, RECALL_BATCH_SIZE, RECALL_REFILL_PAGES,
                        decide_update, indexed_terms, pick_for_display, query_kind, recall_rank, search_terms, shareable_self_activity)
from ..persistence.errors import EventConflictError, StorageError, StateNotFoundError, StorageNotOpenError
from ..persistence.serialization import canonical_json


INDEX_REPAIR_BATCH_SIZE = 500
logger = logging.getLogger(__name__)


class _IndexRowFailure(Exception):
    """仅包装单行工作中的可降级错误；保存点建立/回滚失败不被误吞。"""


def stamp(value):
    return value.isoformat(timespec="microseconds")


def memory_json(record):
    # 默认字段不进入历史草稿/记录的序列化，保留旧回执与 JSON 兼容。
    return record.model_dump_json(exclude={"activity"} if record.activity is None else set())


def public_scope(scope_id):
    if not isinstance(scope_id,str) or not scope_id.strip():
        raise ValueError("公开记忆操作需要明确的非空会话范围")
    return scope_id


def sort_key(record):
    return stamp(record.updated_at)+":"+record.memory_id


class MemoryPersistence:
    def __init__(self, core):
        self.core = core
        self.session = core.session

    def _record(self, row, *, allow_unindexed=False):
        record=MemoryRecord.model_validate_json(row["record_json"])
        expected=self._row(record)
        if allow_unindexed and row.get("index_version")==0:
            expected.pop("index_version")
            if row.get("updated_at")=="":
                expected.pop("updated_at")
        if any(row.get(key)!=value for key,value in expected.items() if key!="record_json"):
            raise StorageError("记忆索引与正文不一致")
        return record

    @staticmethod
    def _row(record):
        return {"memory_id":record.memory_id,"agent_id":record.agent_id,"scope_id":"" if record.visibility=="self" else record.scope_id,
                "kind":record.kind,"visibility":record.visibility,"speaker_id":record.speaker_id,
                "about_id":record.about_id,"claim_type":record.claim_type,"provenance":record.provenance.value,
                "topic_key":record.topic_key,"target_start_at":None if record.target_start_at is None else stamp(record.target_start_at),
                "target_end_at":None if record.target_end_at is None else stamp(record.target_end_at),
                "plan_status":record.plan_status,"sort_key":sort_key(record),"revision":record.revision,"updated_at":stamp(record.updated_at),
                "index_version":INDEX_VERSION,
                "record_json":memory_json(record)}

    async def _replace_terms(self, record):
        await self.session.delete("memory_terms",where={"memory_id":record.memory_id})
        for term in indexed_terms(record):
            await self.session.insert("memory_terms",{"agent_id":record.agent_id,"scope_id":"" if record.visibility=="self" else record.scope_id,
                "memory_id":record.memory_id,"term":term,"sort_key":sort_key(record)})

    async def reconcile_indexes(self):
        """每次启动按 ID 游标遍历待修复行，单行错误隔离且不会在本轮反复重试。"""
        repaired=skipped=batches=0
        samples=[]
        after_id=None

        def skipped_row(row, error_type):
            nonlocal skipped
            skipped+=1
            if len(samples)<5:
                identifier=str(row["memory_id"])
                samples.append(identifier)
                logger.warning("Persona 记忆索引重建跳过异常记录 memory_id=%r error_type=%s；保留原数据，下次启动重试",
                               identifier,error_type)

        while True:
            rows=await self.session.fetch_all("memories",where={"index_version":0},
                after=None if after_id is None else {"memory_id":after_id},
                order_by=("memory_id",),limit=INDEX_REPAIR_BATCH_SIZE)
            if not rows:
                break
            batches+=1
            for row in rows:
                try:
                    record=self._record(row,allow_unindexed=True)
                except StorageNotOpenError:
                    raise
                except (StorageError,ValueError) as exc:
                    skipped_row(row,type(exc).__name__)
                    continue
                try:
                    async with self.session.savepoint():
                        try:
                            await self._replace_terms(record)
                            changed=await self.session.update("memories",{"updated_at":stamp(record.updated_at),"index_version":INDEX_VERSION},
                                where={"memory_id":record.memory_id,"revision":record.revision,"index_version":0})
                            if changed!=1:
                                raise StorageError("记忆索引回填冲突")
                        except StorageNotOpenError:
                            raise
                        except (StorageError,ValueError) as exc:
                            raise _IndexRowFailure(type(exc).__name__) from exc
                except _IndexRowFailure as exc:
                    skipped_row(row,str(exc))
                    continue
                repaired+=1
            # 即使整批失败也推进游标，留待下一次启动，而非在同一 while 中撞回原行。
            after_id=rows[-1]["memory_id"]
        if skipped:
            logger.warning("Persona 记忆索引已降级运行：本次跳过 %d 条，修复 %d 条；异常记录不参与召回，可用 /persona_memory pending 查看当前会话",
                           skipped,repaired)
        return MemoryIndexRepairReport(repaired=repaired,skipped=skipped,batches=batches,sample_ids=tuple(samples))

    async def get(self, agent_id, scope_id, memory_id):
        public_scope(scope_id)
        row=await self.session.fetch_one("memories",where={"agent_id":agent_id,"scope_id":scope_id,"visibility":"scope","memory_id":memory_id})
        if row is None:
            return None
        if row["index_version"]!=INDEX_VERSION:
            raise StorageError("这条记忆索引待修复，暂不参与召回；可用 /persona_memory pending 查看或 delete 删除")
        return self._record(row)

    async def list(self, agent_id, scope_id, *, limit=10, before_id=None):
        public_scope(scope_id)
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError("记忆列表数量必须在 1 到 100 之间")
        before=None
        if before_id:
            cursor=await self.get(agent_id,scope_id,before_id)
            if cursor is None:
                raise ValueError("分页游标不属于当前会话或已经删除")
            before={"sort_key":sort_key(cursor)}
        rows=await self.session.fetch_all("memories",where={"agent_id":agent_id,"scope_id":scope_id,"visibility":"scope","index_version":INDEX_VERSION},
            before=before,order_by=("sort_key",),descending=True,limit=limit)
        return tuple(self._record(row) for row in rows)

    async def pending(self, agent_id, scope_id, *, after_id=None):
        public_scope(scope_id)
        return await self.session.fetch_all("memories",where={"agent_id":agent_id,"scope_id":scope_id,"visibility":"scope","index_version":0},
            after=None if not after_id else {"memory_id":after_id},order_by=("memory_id",),limit=10)

    async def list_internal(self, agent_id, *, limit=10, before_id=None):
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError("内部记忆列表数量必须在 1 到 100 之间")
        before=None
        if before_id:
            row=await self.session.fetch_one("memories",where={"agent_id":agent_id,"scope_id":"","visibility":"self","memory_id":before_id})
            if row is None:
                raise ValueError("内部记忆游标不存在")
            before={"sort_key":row["sort_key"]}
        rows=await self.session.fetch_all("memories",where={"agent_id":agent_id,"scope_id":"","visibility":"self","index_version":INDEX_VERSION},
            before=before,order_by=("sort_key",),descending=True,limit=limit)
        return tuple(self._record(row) for row in rows)

    async def _receipt(self, agent_id, event, key, request):
        # 回执只保存请求摘要；删除正文后不会在回执里残留另一份记忆内容。
        digest=canonical_json({"sha256":sha256(canonical_json(request).encode()).hexdigest()})
        where={"agent_id":agent_id,"source_event_id":event.event_id,"draft_key":key}
        row=await self.session.fetch_one("memory_receipts",where=where)
        if row is not None:
            if row["request_json"]!=digest:
                raise EventConflictError("同一来源与记忆操作键的内容发生变化")
            return MemoryWriteResult.model_validate_json(row["result_json"]),where,digest
        return None,where,digest

    async def _finish(self, where, digest, result):
        await self.session.insert("memory_receipts",{**where,"request_json":digest,"result_json":result.model_dump_json()})
        return result

    async def observe(self, agent_id, event, settings, *, text=None, explicit=False, allow_capture=True):
        raw=event.payload.get("text") if text is None else text
        key="manual:primary" if explicit else "auto:primary"
        previous,where,digest=await self._receipt(agent_id,event,key,{"text":raw,"explicit":explicit})
        if previous is not None:
            return previous
        if (not allow_capture or not settings.enabled or (not explicit and not settings.auto_capture)
                or (settings.private_only and bool(event.payload.get("group_id")))):
            return await self._finish(where,digest,MemoryWriteResult(action="ignored"))
        draft=extract_memory(event,settings,text=text,explicit=explicit)
        if draft is None:
            return await self._finish(where,digest,MemoryWriteResult(action="ignored"))
        result=await self._write(agent_id,event,draft,settings,key)
        return await self._finish(where,digest,result)

    async def write_draft(self, agent_id, event, draft, settings):
        draft=MemoryDraft.model_validate(draft.model_dump())
        if draft.activity is not None:
            raise ValueError("活动经历只能通过已提交活动的专用入口写入")
        key="draft:"+draft.draft_key
        previous,where,digest=await self._receipt(agent_id,event,key,draft.model_dump(mode="json",exclude={"activity"}))
        if previous is not None:
            return previous
        if not settings.enabled:
            return await self._finish(where,digest,MemoryWriteResult(action="ignored"))
        result=await self._write(agent_id,event,draft,settings,key)
        return await self._finish(where,digest,result)

    async def write_experience(self, agent_id, event, details, text, importance, settings, *, allow_capture=True):
        details=ActivityMemoryContext.model_validate(details.model_dump())
        phase={"ACTIVITY.stage_completed":"stage","ACTIVITY.completed":"completed","ACTIVITY.failed":"failed"}.get(event.event_type)
        if (phase!=details.phase or event.activity_id!=details.activity_id or event.payload.get("agent_id")!=agent_id
                or event.payload.get("definition_id")!=details.definition_id):
            raise ValueError("经历入口与原活动事件身份不一致")
        body=event.payload.get("feedback")
        if isinstance(body,dict):
            task=body.get("task") or {"success":"success","failure":"failure"}.get(body.get("outcome"),"neutral")
            if body.get("checkpoint_id")!=details.checkpoint_id or body.get("summary")!=text or task!=details.task:
                raise ValueError("经历内容与原活动反馈不一致")
        elif details.phase=="stage" or details.checkpoint_id is not None:
            raise ValueError("阶段经历必须有原始内容反馈")
        visibility="self" if event.scope_id is None else "scope"
        key="activity:experience"
        # 重要度可来自既有结构化评价，重投仍以原事件及 checkpoint 为身份，不重跑评价。
        previous,where,digest=await self._receipt(agent_id,event,key,{"details":details.model_dump(mode="json"),"text":text})
        if previous is not None:
            return previous
        is_group=bool(event.payload.get("group_id")) or bool(event.scope_id and ":GroupMessage:" in event.scope_id)
        if not allow_capture or not settings.enabled or not settings.activity_memories_enabled or (visibility=="scope" and settings.private_only and is_group):
            return await self._finish(where,digest,MemoryWriteResult(action="ignored"))
        draft=MemoryDraft(kind="story" if event.provenance.value in ("simulated","inferred") else "episodic",
            visibility=visibility,scope_id=event.scope_id,speaker_id=None,about_id="agent:"+agent_id,
            claim_type="event",occurred_at=event.timestamp,valid_from=event.timestamp,text=text,
            keywords=tuple(dict.fromkeys((*search_terms(details.name)[:1],*search_terms(text)[-3:])))[:4],
            source_event_id=event.event_id,provenance=event.provenance,importance=importance,
            topic_key="activity:"+sha256(details.activity_id.encode()).hexdigest(),
            detail_key=details.phase+":"+(details.checkpoint_id or "terminal"),activity=details)
        result=await self._write(agent_id,event,draft,settings,key,activity_source=True)
        return await self._finish(where,digest,result)

    async def _write(self, agent_id, event, draft, settings, operation_key, *, activity_source=False):
        if await self.core.get_state(agent_id) is None:
            raise StateNotFoundError("角色不存在")
        scoped=draft.visibility=="scope" and draft.scope_id==event.scope_id and event.scope_id is not None
        internal=activity_source and draft.visibility=="self" and draft.scope_id is None and event.scope_id is None
        speaker=None if activity_source else actor_id(event)
        if (not (scoped or internal) or draft.source_event_id!=event.event_id or draft.provenance!=event.provenance
                or draft.occurred_at!=event.timestamp or draft.speaker_id!=speaker
                or (not activity_source and settings.private_only and bool(event.payload.get("group_id")))):
            raise ValueError("记忆的权限、来源、人物或时间与宿主事件不一致")
        try:
            cutoff=draft.occurred_at-timedelta(seconds=settings.merge_window_seconds)
        except OverflowError:
            cutoff=datetime.min.replace(tzinfo=timezone.utc)
        filters={"agent_id":agent_id,"scope_id":draft.scope_id,"about_id":draft.about_id,
                 "claim_type":draft.claim_type,"kind":draft.kind,"provenance":draft.provenance.value,"index_version":INDEX_VERSION}
        if draft.topic_key:
            filters["topic_key"]=draft.topic_key
        if draft.claim_type=="plan" and draft.update_hint!="correct":
            filters.update(target_start_at=None if draft.target_start_at is None else stamp(draft.target_start_at),
                           target_end_at=None if draft.target_end_at is None else stamp(draft.target_end_at))
        if activity_source:
            old,action,content=None,"created",draft
        else:
            rows=await self.session.fetch_all("memories",where=filters,
                at_least={"updated_at":stamp(cutoff)},at_most={"updated_at":stamp(draft.occurred_at)},
                order_by=("updated_at","memory_id"),descending=True,limit=settings.candidate_limit)
            candidates=[self._record(row) for row in rows]
            # 活动经历只由源事件决定，不让普通草稿归并改写其阶段身份。
            old,action,content=decide_update(draft,[item for item in candidates if item.activity is None],settings)
        identifier=(f"memory:{uuid5(NAMESPACE_URL,canonical_json([agent_id,event.event_id,operation_key]))}"
                    if old is None else old.memory_id)
        record=MemoryRecord(**{**content.model_dump(),
            "importance":draft.importance if old is None else max(old.importance,draft.importance),
            "memory_id":identifier,"agent_id":agent_id,"revision":1 if old is None else old.revision+1,
            "created_at":event.timestamp if old is None else old.created_at,"updated_at":event.timestamp})
        if old is None:
            await self.session.insert("memories",self._row(record))
        else:
            changed=await self.session.update("memories",self._row(record),where={"memory_id":identifier,"revision":old.revision})
            if changed!=1:
                raise StorageError("记忆修订冲突")
        await self.session.insert("memory_revisions",{"memory_id":identifier,"revision":record.revision,
            "action":action,"source_event_id":event.event_id,"record_json":memory_json(record)})
        await self.session.insert("memory_sources",{"memory_id":identifier,"source_event_id":event.event_id,
            "draft_key":operation_key,"occurred_at":stamp(event.timestamp)})
        await self._replace_terms(record)
        return MemoryWriteResult(action=action,memory_id=identifier,revision=record.revision)

    async def delete(self, agent_id, event, memory_id):
        public_scope(event.scope_id)
        previous,where,digest=await self._receipt(agent_id,event,"delete",{"memory_id":memory_id})
        if previous is not None:
            return previous
        row=await self.session.fetch_one("memories",where={"agent_id":agent_id,"scope_id":event.scope_id,"visibility":"scope","memory_id":memory_id})
        if row is None:
            raise ValueError("当前会话没有这条记忆")
        for table in ("memory_terms","memory_sources","memory_revisions"):
            await self.session.delete(table,where={"memory_id":memory_id})
        if await self.session.delete("memories",where={"agent_id":agent_id,"scope_id":event.scope_id,"memory_id":memory_id,"revision":row["revision"]})!=1:
            raise StorageError("记忆删除冲突")
        revision=row["revision"] if type(row["revision"]) is int and row["revision"]>=1 else None
        return await self._finish(where,digest,MemoryWriteResult(action="deleted",memory_id=memory_id,revision=revision))

    async def history(self, agent_id, scope_id, memory_id, *, limit=10):
        if await self.get(agent_id,scope_id,memory_id) is None:
            raise ValueError("当前会话没有这条记忆")
        return await self.session.fetch_all("memory_revisions",where={"memory_id":memory_id},
            order_by=("revision",),descending=True,limit=limit)

    async def recall(self, agent_id, scope_id, settings, *, query="", speaker_id=None, now):
        public_scope(scope_id)
        if not settings.enabled:
            return ()
        kind=query_kind(query)
        terms=search_terms(query)[:QUERY_TERM_LIMIT]
        candidates={}
        loaded=set()
        streams=[]
        requested_target=None
        scoped_where={"agent_id":agent_id,"scope_id":scope_id,"visibility":"scope","index_version":INDEX_VERSION}
        shared_where={"agent_id":agent_id,"scope_id":"","visibility":"self","about_id":"agent:"+agent_id,
                      "claim_type":"event","kind":"story","provenance":"simulated","index_version":INDEX_VERSION}

        def accept(rows,base):
            for row in rows:
                item=self._record(row)
                loaded.add(item.memory_id)
                if item.visibility=="scope" or (settings.activity_memories_enabled and shareable_self_activity(item)):
                    old=candidates.get(item.memory_id)
                    candidates[item.memory_id]=(item,base if old is None else max(base,old[1]))

        def continue_stream(table,where,rows,base,record_where=None):
            if len(rows)==settings.candidate_limit:
                streams.append({"table":table,"where":where,"cursor":rows[-1]["sort_key"],
                                "base":base,"record_where":record_where})

        async def recent(where,base):
            rows=await self.session.fetch_all("memories",where=where,
                order_by=("sort_key",),descending=True,limit=settings.candidate_limit)
            accept(rows,base)
            continue_stream("memories",where,rows,base)

        async def load_terms(where,identifiers):
            ordered=sorted(identifier for identifier in identifiers if identifier not in loaded)
            for start in range(0,len(ordered),RECALL_BATCH_SIZE):
                batch=tuple(ordered[start:start+RECALL_BATCH_SIZE])
                rows=await self.session.fetch_all("memories",where=where,where_in={"memory_id":batch},limit=len(batch))
                accept(rows,0.0)

        if kind is not None:
            where={**scoped_where,"claim_type":"event" if kind=="experience" else kind}
            if kind=="experience":
                where["about_id"]="agent:"+agent_id
                await recent(where,2.0)
            else:
                if speaker_id is not None:
                    where["about_id"]=speaker_id
                if kind=="plan":
                    target=target_range(query,now,settings.utc_offset_minutes)
                    requested_target=target
                    lower={"target_end_at":stamp(now)}
                    if target is not None:
                        where.update(target_start_at=stamp(target[0]),target_end_at=stamp(target[1]))
                        lower=None
                    rows=await self.session.fetch_all("memories",where=where,
                        at_least=lower,order_by=("target_end_at","memory_id"),limit=settings.candidate_limit)
                    if target is None:
                        unknown=await self.session.fetch_all("memories",where={**where,"target_end_at":None},
                            order_by=("sort_key",),descending=True,limit=settings.candidate_limit)
                        rows=(*rows,*unknown)
                else:
                    rows=await self.session.fetch_all("memories",where=where,
                        order_by=("sort_key",),descending=True,limit=settings.candidate_limit)
                accept(rows,2.0)
        if settings.activity_memories_enabled and (kind=="experience" or not query.strip()):
            await recent(shared_where,2.0 if kind=="experience" else 1.0)
        if not query.strip():
            await recent(scoped_where,1.0)
        elif kind is None:
            filters=[scoped_where]
            if settings.activity_memories_enabled:
                filters.append(shared_where)
            for where in filters:
                identifiers=set()
                for term in terms:
                    term_where={"agent_id":agent_id,"scope_id":where["scope_id"],"term":term}
                    rows=await self.session.fetch_all("memory_terms",where=term_where,
                        order_by=("sort_key",),descending=True,limit=settings.candidate_limit)
                    identifiers.update(row["memory_id"] for row in rows)
                    continue_stream("memory_terms",term_where,rows,0.0,where)
                await load_terms(where,identifiers)

        def select():
            valid=[]
            for record,score in candidates.values():
                scoped=record.visibility=="scope" and record.scope_id==scope_id
                own=settings.activity_memories_enabled and shareable_self_activity(record)
                if record.agent_id!=agent_id or not (scoped or own) or record.updated_at>now:
                    continue
                if record.valid_from is not None and record.valid_from>now:
                    continue
                if record.valid_until is not None and record.valid_until<=now:
                    continue
                if kind=="plan" and requested_target is None and record.target_end_at is not None and record.target_end_at<=now:
                    continue
                rank=recall_rank(record,terms,now=now,half_life_days=settings.recall_half_life_days,base_score=score)
                valid.append((record,rank))
            valid.sort(key=lambda item:(item[1],item[0].updated_at,item[0].importance,item[0].memory_id),reverse=True)
            return pick_for_display(valid,settings.recall_limit,query=query)

        selected=select()
        # 所有会话/self/词项流共用八页补取预算；满额后立即停止，不扫完整历史。
        for _ in range(RECALL_REFILL_PAGES):
            if len(selected)>=settings.recall_limit or not streams:
                break
            stream=streams.pop(0)
            rows=await self.session.fetch_all(stream["table"],where=stream["where"],
                before={"sort_key":stream["cursor"]},order_by=("sort_key",),descending=True,limit=settings.candidate_limit)
            if stream["table"]=="memory_terms":
                await load_terms(stream["record_where"],{row["memory_id"] for row in rows})
            else:
                accept(rows,stream["base"])
            continue_stream(stream["table"],stream["where"],rows,stream["base"],stream["record_where"])
            selected=select()
        return selected
