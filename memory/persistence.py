"""事务内的会话记忆规则；不执行 SQL，不重新结算角色效果。"""

import logging
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5
from .models import MemoryDraft, MemoryRecord, MemoryWriteResult, MemoryIndexRepairReport
from .extraction import actor_id, extract_memory, target_range
from .retrieval import (INDEX_VERSION, QUERY_TERM_LIMIT, RECALL_BATCH_SIZE,
                        decide_update, indexed_terms, pick_for_display, query_kind, recall_rank, search_terms)
from ..persistence.errors import EventConflictError, StorageError, StateNotFoundError, StorageNotOpenError
from ..persistence.serialization import canonical_json


INDEX_REPAIR_BATCH_SIZE = 500
logger = logging.getLogger(__name__)


class _IndexRowFailure(Exception):
    """仅包装单行工作中的可降级错误；保存点建立/回滚失败不被误吞。"""


def stamp(value):
    return value.isoformat(timespec="microseconds")


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
        return {"memory_id":record.memory_id,"agent_id":record.agent_id,"scope_id":record.scope_id,
                "kind":record.kind,"visibility":record.visibility,"speaker_id":record.speaker_id,
                "about_id":record.about_id,"claim_type":record.claim_type,"provenance":record.provenance.value,
                "topic_key":record.topic_key,"target_start_at":None if record.target_start_at is None else stamp(record.target_start_at),
                "target_end_at":None if record.target_end_at is None else stamp(record.target_end_at),
                "plan_status":record.plan_status,"sort_key":sort_key(record),"revision":record.revision,"updated_at":stamp(record.updated_at),
                "index_version":INDEX_VERSION,
                "record_json":record.model_dump_json()}

    async def _replace_terms(self, record):
        await self.session.delete("memory_terms",where={"memory_id":record.memory_id})
        for term in indexed_terms(record):
            await self.session.insert("memory_terms",{"agent_id":record.agent_id,"scope_id":record.scope_id,
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
        row=await self.session.fetch_one("memories",where={"agent_id":agent_id,"scope_id":scope_id,"memory_id":memory_id})
        if row is None:
            return None
        if row["index_version"]!=INDEX_VERSION:
            raise StorageError("这条记忆索引待修复，暂不参与召回；可用 /persona_memory pending 查看或 delete 删除")
        return self._record(row)

    async def list(self, agent_id, scope_id, *, limit=10, before_id=None):
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError("记忆列表数量必须在 1 到 100 之间")
        before=None
        if before_id:
            cursor=await self.get(agent_id,scope_id,before_id)
            if cursor is None:
                raise ValueError("分页游标不属于当前会话或已经删除")
            before={"sort_key":sort_key(cursor)}
        rows=await self.session.fetch_all("memories",where={"agent_id":agent_id,"scope_id":scope_id,"index_version":INDEX_VERSION},
            before=before,order_by=("sort_key",),descending=True,limit=limit)
        return tuple(self._record(row) for row in rows)

    async def pending(self, agent_id, scope_id, *, after_id=None):
        return await self.session.fetch_all("memories",where={"agent_id":agent_id,"scope_id":scope_id,"index_version":0},
            after=None if not after_id else {"memory_id":after_id},order_by=("memory_id",),limit=10)

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
        key="draft:"+draft.draft_key
        previous,where,digest=await self._receipt(agent_id,event,key,draft.model_dump(mode="json"))
        if previous is not None:
            return previous
        if not settings.enabled:
            return await self._finish(where,digest,MemoryWriteResult(action="ignored"))
        result=await self._write(agent_id,event,draft,settings,key)
        return await self._finish(where,digest,result)

    async def _write(self, agent_id, event, draft, settings, operation_key):
        if await self.core.get_state(agent_id) is None:
            raise StateNotFoundError("角色不存在")
        if (draft.visibility!="scope" or draft.scope_id!=event.scope_id or event.scope_id is None
                or draft.source_event_id!=event.event_id or draft.provenance!=event.provenance
                or draft.occurred_at!=event.timestamp or draft.speaker_id!=actor_id(event)
                or (settings.private_only and bool(event.payload.get("group_id")))):
            raise ValueError("记忆的权限、来源、人物或时间与宿主事件不一致；首版只开放 scope")
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
        rows=await self.session.fetch_all("memories",where=filters,
            at_least={"updated_at":stamp(cutoff)},at_most={"updated_at":stamp(draft.occurred_at)},
            order_by=("updated_at","memory_id"),descending=True,limit=settings.candidate_limit)
        old,action,content=decide_update(draft,[self._record(row) for row in rows],settings)
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
            "action":action,"source_event_id":event.event_id,"record_json":record.model_dump_json()})
        await self.session.insert("memory_sources",{"memory_id":identifier,"source_event_id":event.event_id,
            "draft_key":operation_key,"occurred_at":stamp(event.timestamp)})
        await self._replace_terms(record)
        return MemoryWriteResult(action=action,memory_id=identifier,revision=record.revision)

    async def delete(self, agent_id, event, memory_id):
        previous,where,digest=await self._receipt(agent_id,event,"delete",{"memory_id":memory_id})
        if previous is not None:
            return previous
        row=await self.session.fetch_one("memories",where={"agent_id":agent_id,"scope_id":event.scope_id,"memory_id":memory_id})
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
        if not settings.enabled:
            return ()
        kind=query_kind(query)
        terms=search_terms(query)[:QUERY_TERM_LIMIT]
        candidates={}
        requested_target=None
        if kind is not None:
            where={"agent_id":agent_id,"scope_id":scope_id,"claim_type":kind,"index_version":INDEX_VERSION}
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
                rows=await self.session.fetch_all("memories",where=where,order_by=("sort_key",),descending=True,limit=settings.candidate_limit)
            candidates.update((row["memory_id"],(self._record(row),2.0)) for row in rows)
        if not query.strip():
            candidates.update((m.memory_id,(m,1.0)) for m in await self.list(agent_id,scope_id,limit=settings.candidate_limit))
        elif kind is None:
            identifiers=set()
            for term in terms:
                rows=await self.session.fetch_all("memory_terms",where={"agent_id":agent_id,"scope_id":scope_id,"term":term},
                    order_by=("sort_key",),descending=True,limit=settings.candidate_limit)
                identifiers.update(row["memory_id"] for row in rows)
            # 每词有限索引结果形成有界候选池，批量加载后评分；不按原始命中次数提前截断。
            ordered=sorted(identifiers)
            for start in range(0,len(ordered),RECALL_BATCH_SIZE):
                batch=tuple(ordered[start:start+RECALL_BATCH_SIZE])
                rows=await self.session.fetch_all("memories",where={"agent_id":agent_id,"scope_id":scope_id,"index_version":INDEX_VERSION},
                    where_in={"memory_id":batch},limit=len(batch))
                candidates.update((row["memory_id"],(self._record(row),0.0)) for row in rows)
        valid=[]
        for record,score in candidates.values():
            if record.visibility!="scope" or record.scope_id!=scope_id or record.updated_at>now:
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
        return pick_for_display(valid,settings.recall_limit)
