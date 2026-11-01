"""已提交活动到记忆的本地闭环；由用户手动运行，不访问模型或 QQ。"""

import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from persona_agent.activity.executor import ActivityContext, ActivityExecutionResult
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.feedback import ActivityFeedback, attach_completion_feedback, build_feedback_event
from persona_agent.activity.manager import ActivityManager
from persona_agent.activity.models import ActivityDefinition, ActivityRecord, ActivityStatus, ExecutionMode, create_activity_instance
from persona_agent.activity.registry import ExecutorRegistry
from persona_agent.activity.ticks import build_activity_tick
from persona_agent.application.persistence import PersonaStore
from persona_agent.cognition.evaluation import EvaluationSettings
from persona_agent.cognition.prompt_builder import (ChatContextSettings, ChatSnapshot, ContextActivity, ContextActivityMemory,
    ContextFeedback, ContextMemory, build_prompt_context)
from persona_agent.cognition.semantic import ExperienceAssessment
from persona_agent.core.events import AgentEvent
from persona_agent.core.scheduler import ScheduleStatus
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.memory.extraction import actor_id, extract_memory
from persona_agent.memory.models import MemoryDraft, MemorySettings
from persona_agent.memory.persistence import MemoryPersistence, memory_json
from persona_agent.memory.retrieval import query_kind
from persona_agent.persistence.errors import EventConflictError
from persona_agent.persistence.serialization import canonical_json
from persona_agent.persona.emotion import EmotionDynamics, EmotionState, VAD
from persona_agent.persona.needs import NeedRates, NeedState
from hashlib import sha256

TIME=datetime(2026,11,1,10,tzinfo=timezone.utc)
SCOPE="qq-1:FriendMessage:alice"


def experience(**changes):
    return ExperienceAssessment(**{**dict(interest=1,frustration=0,progress=.5,control=0,
        pleasantness=.5,activation=.2,importance=.8,uncertain=False),**changes})


def message(identifier="question",text="你刚才做了什么",scope=SCOPE):
    return AgentEvent(event_id="qq:"+identifier,event_type="SOCIAL.message_received",timestamp=TIME,
        source="astrbot.aiocqhttp",provenance="observed",scope_id=scope,
        payload={"platform_id":"qq-1","sender_id":"alice","group_id":"","text":text})


class ActivityMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory()
        self.path=Path(self.directory.name)/"activity-memory.sqlite3"
        self.agent="persona-001"
        self.settings=MemorySettings()
        self.store=PersonaStore(SQLiteStorage(self.path),memory=self.settings)
        await self.store.open()
        zero=VAD(valence=0,arousal=0,dominance=0)
        self.initial=AgentState(agent_id=self.agent,updated_at=TIME,
            needs=NeedState(energy=40,satiety=70,boredom=30,social_need=50),
            emotion=EmotionState(current=zero,mood=zero,baseline=zero,updated_at=TIME,
                dynamics=EmotionDynamics(emotion_half_life_seconds=300,mood_half_life_seconds=7200,mood_impulse_ratio=.1)))
        await self.store.create_state(self.initial)
        self.definition=ActivityDefinition(definition_id="rest",name="休息",supported_modes=["simulated"],
            estimated_duration_seconds=10,completion_needs_delta={"energy":10})
        instance=create_activity_instance(self.definition,activity_id="activity-one",agent_id=self.agent,
            mode=ExecutionMode.SIMULATED,created_at=TIME)
        await self.store.create_activity(self.definition,instance)
        registry=ExecutorRegistry()
        registry.register("rest",ExecutionMode.SIMULATED,SimulatedActivityExecutor())
        self.manager=ActivityManager(registry)
        context=self.context(instance,0)
        result=await self.manager.start(context)
        self.running=await self.store.commit_activity_result("start",context,result)

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    def context(self,instance,seconds):
        return ActivityContext(definition=self.definition,instance=instance,now=TIME+timedelta(seconds=seconds))

    async def report(self,checkpoint="one",scope=SCOPE,**changes):
        feedback=ActivityFeedback(**{**dict(checkpoint_id=checkpoint,task="partial",summary="完成了第一段内容"),**changes})
        return await self.store.report_activity_feedback(self.agent,"activity-one",feedback,now=TIME,scope_id=scope)

    async def memories(self,scope=SCOPE):
        async with self.store._repositories() as (core,_):
            return await MemoryPersistence(core).list(self.agent,scope)

    async def snapshot(self,scope=SCOPE,query="你刚才做了什么",at=None):
        return await self.store.chat_snapshot(self.agent,scope,now=at or TIME,rates=NeedRates(energy=0,satiety=0,boredom=0,social_need=0),labels={"rest":"休息"},
            query=query,speaker_id=actor_id(message()))

    async def completion(self,*,summary=None,scope=None):
        context=self.context(self.running.instance,10)
        result=await self.manager.step(context)
        if summary is not None or scope is not None:
            event=AgentEvent(**{**result.events[0].model_dump(),"scope_id":scope})
            if summary is not None:
                event=attach_completion_feedback(event,ActivityFeedback(checkpoint_id="whole",task="success",
                    scope="completion",summary=summary))
            result=ActivityExecutionResult(instance=result.instance,events=(event,))
        return context,result

    async def count(self,table):
        async with self.store._repositories() as (core,_):
            return len(await core.session.fetch_all(table,limit=100))

    async def test_stage_memory_preserves_identity_and_does_not_finish_or_reward(self):
        event=await self.report(experience=experience())
        item=(await self.memories())[0]
        self.assertEqual(item.activity.phase,"stage")
        self.assertEqual(item.activity.task,"partial")
        self.assertEqual(item.activity.checkpoint_id,"one")
        self.assertEqual(item.activity.activity_id,"activity-one")
        self.assertEqual((item.about_id,item.speaker_id),("agent:"+self.agent,None))
        self.assertEqual((item.kind,item.provenance.value),("story","simulated"))
        self.assertEqual((item.source_event_id,item.occurred_at),(event.event_id,TIME))
        self.assertEqual(item.importance,.8)
        self.assertEqual(await self.store.get_activity(self.agent,"activity-one"),self.running)
        self.assertEqual(await self.store.get_state(self.agent),self.initial)
        self.assertEqual(await self.count("state_settlements"),0)

    async def test_checkpoint_replay_restart_and_conflict(self):
        event=await self.report()
        original=await self.memories()
        await self.store.close()
        await self.store.open()
        self.assertEqual(await self.report(),event)
        self.assertEqual(await self.memories(),original)
        self.assertEqual(await self.count("memory_sources"),1)
        self.assertEqual(await self.count("memory_revisions"),1)
        with self.assertRaises(EventConflictError):
            await self.report(summary="改写了同一个 checkpoint")
        self.assertEqual(await self.memories(),original)

    async def test_different_checkpoints_with_same_text_are_distinct_stages(self):
        await self.report("one")
        await self.report("two")
        items=await self.memories()
        self.assertEqual(len(items),2)
        self.assertEqual({m.activity.checkpoint_id for m in items},{"one","two"})
        self.assertEqual({m.activity.activity_id for m in items},{"activity-one"})

    async def test_scoped_experience_recall_does_not_confuse_user_plan(self):
        event=await self.report()
        await self.store.record_rule_message(self.agent,message("plan","我明天会出门"),now=TIME)
        snapshot=await self.snapshot()
        self.assertEqual(len(snapshot.memories),1)
        self.assertEqual(snapshot.memories[0].source_event_id,event.event_id)
        context=build_prompt_context(snapshot,ChatContextSettings())
        data=json.loads(context.user_text.removeprefix("persona_state="))
        self.assertEqual(data["memories"][0]["activity"]["phase"],"stage")
        self.assertEqual(data["memories"][0]["activity"]["id"],"activity-one")
        self.assertEqual(data["memories"][0]["provenance"],"simulated")
        self.assertNotIn("feedback",data)
        self.assertEqual(context.memory_source_event_ids,(event.event_id,))
        self.assertEqual(context.source_event_ids,())
        plans=await self.snapshot(query="我未来的计划是什么")
        self.assertEqual([m.claim_type for m in plans.memories],["plan"])
        self.assertEqual((await self.snapshot(scope="other")).memories,())
        self.assertEqual(await self.store.get_state(self.agent),self.initial)

    async def test_unscoped_experience_is_internal_and_all_public_paths_reject_empty_scope(self):
        await self.report(scope=None,summary="无会话的内部经历")
        internal=await self.store.list_internal_memories(self.agent)
        self.assertEqual(len(internal),1)
        item=internal[0]
        self.assertEqual((item.visibility,item.scope_id),("self",None))
        self.assertEqual(await self.memories(),())
        self.assertEqual((await self.snapshot()).memories,())
        async with self.store._repositories() as (core,_):
            repository=MemoryPersistence(core)
            self.assertIsNone(await repository.get(self.agent,SCOPE,item.memory_id))
            self.assertEqual(await repository.list_internal("other"),())
            for scope in (None,""," "):
                for call in (lambda:repository.list(self.agent,scope),
                             lambda:repository.get(self.agent,scope,item.memory_id),
                             lambda:repository.pending(self.agent,scope),
                             lambda:repository.recall(self.agent,scope,self.settings,now=TIME)):
                    with self.assertRaises(ValueError):await call()
        with self.assertRaises(ValueError):
            await self.store.memory_operation(self.agent,message(scope=""),"delete",item.memory_id,self.settings,now=TIME)

    async def test_scoped_delete_cleans_content_and_replay_does_not_resurrect_it(self):
        await self.report()
        item=(await self.memories())[0]
        await self.store.memory_operation(self.agent,message("delete"),"delete",item.memory_id,self.settings,now=TIME)
        self.assertEqual(await self.memories(),())
        await self.report()
        self.assertEqual(await self.memories(),())
        for table in ("memories","memory_sources","memory_revisions","memory_terms"):
            self.assertEqual(await self.count(table),0)

    async def test_group_restriction_and_explicit_opt_in(self):
        scope="qq-1:GroupMessage:123"
        await self.report("blocked",scope=scope)
        self.assertEqual(await self.memories(scope),())
        self.store._memory=MemorySettings(private_only=False)
        await self.report("allowed",scope=scope)
        self.assertEqual(len(await self.memories(scope)),1)
        self.assertEqual(await self.memories(),())

    async def test_no_progress_pause_resume_cancel_memory(self):
        record=self.running
        for operation,seconds in (("step",2),("pause",3),("resume",4),("stop",5)):
            context=self.context(record.instance,seconds)
            result=await getattr(self.manager,operation)(context)
            record=await self.store.commit_activity_result(operation,context,result)
        self.assertEqual(record.instance.status,ActivityStatus.CANCELLED)
        self.assertEqual(await self.count("memories"),0)
        self.assertEqual(await self.count("memory_receipts"),0)
        self.assertEqual(await self.store.get_state(self.agent),self.initial)

    async def test_bare_terminal_is_minimal_internal_fact_and_reward_only_once(self):
        context,result=await self.completion()
        await self.store.commit_activity_result("step",context,result)
        item=(await self.store.list_internal_memories(self.agent))[0]
        self.assertEqual(item.text,"活动“休息”完成。")
        self.assertEqual(item.activity.phase,"completed")
        self.assertIsNone(item.activity.checkpoint_id)
        state=await self.store.get_state(self.agent)
        self.assertEqual((state.needs.energy,state.version),(50,1))
        await self.store.close()
        await self.store.open()
        await self.store.commit_activity_result("step",context,result)
        self.assertEqual(await self.store.get_state(self.agent),state)
        self.assertEqual(await self.store.list_internal_memories(self.agent),(item,))
        self.assertEqual(await self.count("state_settlements"),1)

    async def test_own_simulation_is_recalled_in_each_session_with_explicit_owner(self):
        context,result=await self.completion()
        await self.store.commit_activity_result("step",context,result)
        original=(await self.store.list_internal_memories(self.agent))[0]
        state=await self.store.get_state(self.agent)
        before={table:await self.count(table) for table in ("agent_events","state_settlements","memories","memory_sources","memory_revisions")}
        for scope in (SCOPE,"qq-1:FriendMessage:bob"):
            snapshot=await self.snapshot(scope=scope,at=context.now)
            self.assertEqual(len(snapshot.memories),1)
            self.assertIsNone(snapshot.memories[0].scope_id)
            self.assertEqual(snapshot.memories[0].visibility,"self")
            prompt=build_prompt_context(snapshot,ChatContextSettings())
            data=json.loads(prompt.user_text.removeprefix("persona_state="))
            memory=data["memories"][0]
            self.assertEqual((memory["owner"],memory["provenance"]),("agent","simulated"))
            self.assertEqual(memory["about"],"agent:"+self.agent)
            self.assertEqual(memory["text"],"活动“休息”完成。")
            self.assertEqual(prompt.memory_source_event_ids,(result.events[0].event_id,))
            self.assertLessEqual(prompt.char_count,1000)
        await self.store.close()
        await self.store.open()
        self.assertEqual((await self.snapshot(at=context.now)).memories[0].memory_id,original.memory_id)
        self.assertEqual(await self.store.list_internal_memories(self.agent),(original,))
        self.assertEqual(await self.store.get_state(self.agent),state)
        self.assertEqual({table:await self.count(table) for table in before},before)

    async def test_eating_and_playing_facts_are_not_limited_to_rest(self):
        for definition_id,name in (("eat","进食"),("play","玩")):
            with self.subTest(activity=name):
                definition=ActivityDefinition(definition_id=definition_id,name=name,supported_modes=["simulated"],
                    estimated_duration_seconds=1)
                instance=create_activity_instance(definition,activity_id=definition_id+"-own",agent_id=self.agent,
                    mode=ExecutionMode.SIMULATED,created_at=TIME)
                await self.store.create_activity(definition,instance)
                executor=SimulatedActivityExecutor()
                start=ActivityContext(definition=definition,instance=instance,now=TIME)
                running=await self.store.commit_activity_result("start",start,await executor.start(start))
                finish=ActivityContext(definition=definition,instance=running.instance,now=TIME+timedelta(seconds=1))
                result=await executor.step(finish)
                await self.store.commit_activity_result("step",finish,result)
                snapshot=await self.snapshot(query=name,at=finish.now)
                self.assertEqual(len(snapshot.memories),1)
                self.assertEqual(snapshot.memories[0].text,f"活动“{name}”完成。")
                context=build_prompt_context(snapshot,ChatContextSettings())
                self.assertIn('"owner":"agent"',context.user_text)
        self.assertEqual(await self.store.get_state(self.agent),self.initial)

    async def test_own_simulation_supports_keyword_and_recent_context(self):
        context,result=await self.completion()
        await self.store.commit_activity_result("step",context,result)
        for query in ("休息",""):
            snapshot=await self.snapshot(query=query,at=context.now)
            self.assertEqual([m.source_event_id for m in snapshot.memories],[result.events[0].event_id])

    async def test_own_simulation_does_not_share_private_activity_feedback(self):
        await self.report("private-stage",summary="仅会话 Alice 的个人内容")
        await self.report("unknown-stage",scope=None,summary="无归属的秘密摘要")
        context,result=await self.completion()
        await self.store.commit_activity_result("step",context,result)
        for query in ("你刚才做了什么","个人内容",""):
            snapshot=await self.snapshot(scope="bob",query=query,at=context.now)
            prompt=build_prompt_context(snapshot,ChatContextSettings())
            self.assertNotIn("个人内容",prompt.user_text)
            self.assertNotIn("秘密摘要",prompt.user_text)
            self.assertTrue(all(m.source_event_id==result.events[0].event_id for m in snapshot.memories))

    async def test_unscoped_completion_summary_is_not_automatically_shared(self):
        context,result=await self.completion(summary="无归属的用户私人反馈")
        await self.store.commit_activity_result("step",context,result)
        self.assertEqual(len(await self.store.list_internal_memories(self.agent)),1)
        for query in ("你刚才做了什么","私人反馈",""):
            self.assertEqual((await self.snapshot(query=query,at=context.now)).memories,())

    async def test_other_agent_and_disabled_context_cannot_recall_own_simulation(self):
        context,result=await self.completion()
        await self.store.commit_activity_result("step",context,result)
        await self.store.create_state(AgentState(**{**self.initial.model_dump(),"agent_id":"other"}))
        other=await self.store.chat_snapshot("other",SCOPE,now=context.now,
            rates=NeedRates(energy=0,satiety=0,boredom=0,social_need=0),labels={},query="你刚才做了什么")
        self.assertEqual(other.memories,())
        for settings in (MemorySettings(enabled=False),MemorySettings(activity_memories_enabled=False)):
            self.store._memory=settings
            for query in ("你刚才做了什么","休息",""):
                self.assertEqual((await self.snapshot(query=query,at=context.now)).memories,())
        self.store._memory=self.settings
        snapshot=await self.store.chat_snapshot(self.agent,SCOPE,now=context.now,
            rates=NeedRates(energy=0,satiety=0,boredom=0,social_need=0),labels={},include_memories=False)
        self.assertEqual(snapshot.memories,())

    async def test_own_simulation_recall_stays_bounded_without_event_history_scan(self):
        context,result=await self.completion()
        await self.store.commit_activity_result("step",context,result)
        async with self.store._repositories() as (core,_):kind=type(core.session)
        original=kind.fetch_all
        async def guarded(session,table,**kwargs):
            where=kwargs.get("where") or {}
            if table in ("memories","memory_terms"):
                self.assertIn(where.get("scope_id"),(SCOPE,""))
                bound=128 if kwargs.get("where_in") else self.settings.candidate_limit
                self.assertLessEqual(kwargs.get("limit",100),bound)
            if table=="agent_events":
                raise AssertionError("own simulation recall scanned historical events")
            return await original(session,table,**kwargs)
        with patch.object(kind,"fetch_all",new=guarded):
            for query in ("你刚才做了什么","休息",""):
                self.assertEqual(len((await self.snapshot(query=query,at=context.now)).memories),1)

    async def test_stage_and_full_summary_keep_separate_phases_under_same_activity(self):
        await self.report()
        context,result=await self.completion(summary="这一整项活动已经结束",scope=SCOPE)
        await self.store.commit_activity_result("step",context,result)
        items=await self.memories()
        self.assertEqual(len(items),2)
        self.assertEqual({m.activity.phase for m in items},{"stage","completed"})
        self.assertEqual({m.activity.activity_id for m in items},{"activity-one"})
        self.assertEqual((await self.store.get_state(self.agent)).needs.energy,50)

    async def test_exhausted_tick_failure_stores_fact_without_error_text_or_reward(self):
        plan=build_activity_tick(self.running)
        # 耗尽处理核对完整的规范计划；沿用真实重试策略，不能改 max_attempts 来加速。
        delivery=await self.store.create_schedule(plan)
        for attempt in range(plan.max_attempts):
            delivery=await self.store.record_schedule_delivery(self.agent,plan.schedule_id,expected_attempts=attempt,
                attempted_at=delivery.next_attempt_at,succeeded=False,error="SECRET_STACK_TRACE")
            if attempt+1<plan.max_attempts:
                self.assertEqual(delivery.status,ScheduleStatus.PENDING)
                self.assertEqual(await self.store.get_activity(self.agent,"activity-one"),self.running)
                self.assertEqual(await self.store.list_internal_memories(self.agent),())
        self.assertEqual(delivery.status,ScheduleStatus.FAILED)
        record=await self.store.get_activity(self.agent,"activity-one")
        self.assertEqual(record.instance.status,ActivityStatus.FAILED)
        items=await self.store.list_internal_memories(self.agent)
        self.assertEqual(len(items),1)
        item=items[0]
        self.assertEqual(item.activity.phase,"failed")
        self.assertEqual(item.activity.task,"failure")
        self.assertEqual(item.text,"活动“休息”执行失败。")
        self.assertNotIn("SECRET_STACK_TRACE",item.model_dump_json())
        prompt=build_prompt_context(await self.snapshot(at=delivery.next_attempt_at),ChatContextSettings())
        data=json.loads(prompt.user_text.removeprefix("persona_state="))
        self.assertEqual(data["memories"][0]["owner"],"agent")
        self.assertEqual(data["memories"][0]["activity"]["phase"],"failed")
        self.assertNotIn("SECRET_STACK_TRACE",prompt.user_text)
        self.assertEqual(await self.store.get_state(self.agent),self.initial)
        await self.store.close()
        await self.store.open()
        self.assertEqual(await self.store.list_internal_memories(self.agent),(item,))

    async def test_completed_summary_replay_after_enabling_does_not_backfill_history(self):
        self.store._memory=None
        context,result=await self.completion(summary="旧版本已经提交的总结",scope=SCOPE)
        await self.store.commit_activity_result("step",context,result)
        self.store._memory=self.settings
        await self.store.commit_activity_result("step",context,result)
        self.assertEqual(await self.memories(),())
        self.assertEqual(await self.count("memory_receipts"),1)

    async def test_activity_switch_is_independent_of_chat_capture_and_no_replay_backfill(self):
        self.store._memory=MemorySettings(activity_memories_enabled=False)
        await self.report("disabled")
        self.store._memory=MemorySettings(auto_capture=False)
        await self.report("disabled")
        self.assertEqual(await self.memories(),())
        await self.report("new")
        self.assertEqual(len(await self.memories()),1)
        await self.store.record_rule_message(self.agent,message("plan","我明天出门"),now=TIME)
        self.assertEqual(len(await self.memories()),1)
        self.store._memory=MemorySettings(enabled=False)
        await self.report("master-off")
        self.assertEqual(len(await self.memories()),1)

    async def test_memory_failure_rolls_back_stage_event_and_feedback_head(self):
        feedback=ActivityFeedback(checkpoint_id="one",task="partial",summary="完成了第一段内容")
        event=build_feedback_event(self.running,feedback,at=TIME,scope_id=SCOPE)
        with patch.object(MemoryPersistence,"_replace_terms",side_effect=RuntimeError("index failed")):
            with self.assertRaises(RuntimeError):await self.report()
        self.assertIsNone(await self.store.get_event(event.event_id))
        for table in ("memories","memory_sources","memory_revisions","memory_receipts","feedback_heads"):
            self.assertEqual(await self.count(table),0)
        self.assertEqual(await self.store.get_state(self.agent),self.initial)
        await self.report()
        self.assertEqual(len(await self.memories()),1)

    async def test_memory_failure_rolls_back_completion_and_rewards_then_retry_succeeds(self):
        context,result=await self.completion(summary="结束总结",scope=SCOPE)
        with patch.object(MemoryPersistence,"_replace_terms",side_effect=RuntimeError("index failed")):
            with self.assertRaises(RuntimeError):await self.store.commit_activity_result("step",context,result)
        self.assertEqual(await self.store.get_activity(self.agent,"activity-one"),self.running)
        self.assertEqual(await self.store.get_state(self.agent),self.initial)
        self.assertIsNone(await self.store.get_event(result.events[0].event_id))
        self.assertEqual(await self.count("state_settlements"),0)
        self.assertEqual(await self.memories(),())
        await self.store.commit_activity_result("step",context,result)
        self.assertEqual((await self.store.get_state(self.agent)).needs.energy,50)
        self.assertEqual(len(await self.memories()),1)

    async def test_evaluation_remains_once_and_memory_is_not_an_extra_effect(self):
        self.store._evaluation=EvaluationSettings(semantic={"enabled":True})
        event=await self.report(experience=experience(),task="failure")
        state=await self.store.get_state(self.agent)
        self.assertEqual(state.needs.energy,40)
        self.assertLess(state.needs.boredom,30)
        self.assertNotEqual(state.emotion.current,self.initial.emotion.current)
        await self.report(experience=experience(),task="failure")
        self.assertEqual(await self.store.get_state(self.agent),state)
        self.assertEqual(await self.count("state_settlements"),1)
        self.assertEqual((await self.memories())[0].source_event_id,event.event_id)

    async def test_successful_deferred_appraisal_keeps_single_original_memory(self):
        self.store._evaluation=EvaluationSettings(semantic={"enabled":True,"timeout_seconds":10})
        event=await self.report(task="failure",evaluation_mode="deferred")
        items=await self.memories()
        await self.store.complete_appraisal(self.agent,event.event_id,experience().model_dump(),now=TIME+timedelta(seconds=1))
        state=await self.store.get_state(self.agent)
        self.assertNotEqual(state,self.initial)
        await self.store.complete_appraisal(self.agent,event.event_id,experience().model_dump(),now=TIME+timedelta(seconds=2))
        self.assertEqual(await self.store.get_state(self.agent),state)
        self.assertEqual(await self.memories(),items)
        self.assertEqual(await self.count("memory_sources"),1)

    async def test_internal_index_repair_preserves_scope_and_does_not_expose_memory(self):
        await self.report(scope=None)
        items=await self.store.list_internal_memories(self.agent)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE memories SET index_version=0 WHERE memory_id=?",(items[0].memory_id,))
            db.commit()
        await self.store.close()
        await self.store.open()
        self.assertEqual(self.store.memory_index_report.repaired,1)
        self.assertEqual(await self.store.list_internal_memories(self.agent),items)
        self.assertEqual((await self.snapshot()).memories,())
        self.assertEqual(await self.memories(),())

    async def test_deferred_and_late_appraisal_do_not_add_memories_or_refresh_importance(self):
        self.store._evaluation=EvaluationSettings(semantic={"enabled":True,"timeout_seconds":10})
        event=await self.report(task="failure",evaluation_mode="deferred")
        original=await self.memories()
        self.assertEqual(original[0].importance,.5)
        self.assertEqual(await self.store.get_state(self.agent),self.initial)
        receipt=await self.store.complete_appraisal(self.agent,event.event_id,experience().model_dump(),now=TIME+timedelta(hours=2))
        self.assertIsNotNone(receipt)
        self.assertEqual(await self.memories(),original)
        self.assertEqual(await self.store.get_state(self.agent),self.initial)
        self.assertEqual(await self.count("memory_sources"),1)

    async def test_generic_draft_cannot_publish_internal_or_forge_activity(self):
        source=message("plan","我明天出门")
        draft=extract_memory(source,self.settings)
        for change in ({"visibility":"self","scope_id":None},{"visibility":"global"}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                await self.store.record_memory_draft(self.agent,source,MemoryDraft(**{**draft.model_dump(),**change}),self.settings)
        await self.report()
        item=(await self.memories())[0]
        with self.assertRaises(ValueError):
            await self.store.record_memory_draft(self.agent,source,MemoryDraft(**{
                **draft.model_dump(),"claim_type":"event","plan_status":None,"activity":item.activity}),self.settings)
        self.assertIsNone(await self.store.get_event(source.event_id))

    async def test_old_draft_receipt_serialization_and_schema_are_compatible(self):
        source=message("plan","我明天出门")
        draft=extract_memory(source,self.settings)
        result=await self.store.record_memory_draft(self.agent,source,draft,self.settings)
        self.assertEqual(await self.store.record_memory_draft(self.agent,source,draft,self.settings),result)
        request=draft.model_dump(mode="json",exclude={"activity"})
        digest=canonical_json({"sha256":sha256(canonical_json(request).encode()).hexdigest()})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],7)
            row=db.execute("SELECT record_json,index_version FROM memories WHERE memory_id=?",(result.memory_id,)).fetchone()
            self.assertNotIn("activity",json.loads(row[0]))
            self.assertEqual(row[1],2)
            self.assertEqual(db.execute("SELECT request_json FROM memory_receipts WHERE draft_key='draft:primary'").fetchone()[0],digest)
        async with self.store._repositories() as (core,_):
            item=await MemoryPersistence(core).get(self.agent,SCOPE,result.memory_id)
        self.assertEqual(memory_json(item),row[0])

    async def test_full_summary_is_stored_but_prompt_keeps_existing_budget(self):
        text="内容"*1000
        event=await self.report(summary=text)
        self.assertEqual((await self.memories())[0].text,text)
        snapshot=await self.snapshot()
        context=build_prompt_context(snapshot,ChatContextSettings())
        self.assertLessEqual(context.char_count,1000)
        self.assertTrue(context.truncated)
        self.assertEqual(context.memory_source_event_ids,(event.event_id,))
        self.assertNotIn(text,context.user_text)


class ActivityMemoryPromptTests(unittest.TestCase):
    def test_experience_intent_does_not_route_future_plans_or_user_history(self):
        for query in ("你刚才做了什么","刚刚在做什么","说说你的最近经历"):
            self.assertEqual(query_kind(query),"experience")
        self.assertEqual(query_kind("我未来的计划是什么"),"plan")
        for query in ("我刚才做了什么","刚才我玩了什么"):
            self.assertNotEqual(query_kind(query),"experience")

    def test_minimum_budget_with_long_identity_emotion_and_running_activity(self):
        snapshot=ChatSnapshot(agent_id="a"*200,scope_id=SCOPE,state_version=0,as_of=TIME,
            needs=NeedState(energy=100,satiety=100,boredom=100,social_need=100),
            emotion=VAD(valence=-.999,arousal=-.999,dominance=-.999),
            activities=(ContextActivity(label="很长的活动名"*20,mode="simulated",status="running",progress=.999),))
        context=build_prompt_context(snapshot,ChatContextSettings(max_chars=500))
        self.assertLessEqual(context.char_count,500)
        json.loads(context.user_text.removeprefix("persona_state="))

    def test_prompt_rejects_self_summaries_other_agents_and_unproven_provenance(self):
        memory=ContextMemory(memory_id="memory:own",scope_id=None,source_event_id="activity:done",about_id="agent:p",
            claim_type="event",text="活动“休息”完成。",provenance="simulated",occurred_at=TIME,visibility="self",
            activity=ContextActivityMemory(activity_id="activity-one",definition_id="rest",name="休息",phase="completed",task="success"))
        base=dict(agent_id="p",scope_id=SCOPE,state_version=0,as_of=TIME,
            needs=NeedState(energy=40,satiety=70,boredom=30,social_need=50),emotion=None)
        accepted=build_prompt_context(ChatSnapshot(**base,memories=(memory,)),ChatContextSettings())
        self.assertIn('"owner":"agent"',accepted.user_text)
        for changes in ({"text":"这是私人摘要"},{"about_id":"agent:other"},{"provenance":"observed"},
                        {"scope_id":"another-scope"},{"visibility":"scope"},{"activity":None}):
            with self.subTest(changes=changes):
                rejected=ContextMemory(**{**memory.model_dump(),**changes})
                context=build_prompt_context(ChatSnapshot(**base,memories=(rejected,)),ChatContextSettings())
                self.assertFalse(context.memory_ids)

    def test_duplicate_feedback_can_fall_back_when_memory_is_trimmed(self):
        memory=ContextMemory(memory_id="memory:one",scope_id=SCOPE,source_event_id="feedback:one",about_id="agent:p",
            claim_type="event",text="阶段内容",provenance="simulated",occurred_at=TIME,
            activity=ContextActivityMemory(activity_id="activity-one",definition_id="rest",name="休息",
                phase="stage",task="partial",checkpoint_id="one"))
        snapshot=ChatSnapshot(agent_id="p",scope_id=SCOPE,state_version=0,as_of=TIME,
            needs=NeedState(energy=40,satiety=70,boredom=30,social_need=50),emotion=None,memories=(memory,),
            feedback=ContextFeedback(scope_id=SCOPE,source_event_id="feedback:one",summary="阶段内容",
                provenance="simulated",occurred_at=TIME))
        context=build_prompt_context(snapshot,ChatContextSettings(max_chars=500))
        data=json.loads(context.user_text.removeprefix("persona_state="))
        self.assertLessEqual(context.char_count,500)
        self.assertFalse(context.memory_ids)
        self.assertEqual(context.source_event_ids,("feedback:one",))
        self.assertEqual(data["feedback"]["summary"],"阶段内容")
