"""活动展示分组、代表选择与有界补取；由用户手动运行，不调用模型。"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from persona_agent.activity.executor import ActivityContext
from persona_agent.activity.executors.simulated import SimulatedActivityExecutor
from persona_agent.activity.feedback import ActivityFeedback
from persona_agent.activity.models import ActivityDefinition, ExecutionMode, create_activity_instance
from persona_agent.application.persistence import PersonaStore
from persona_agent.core.state import AgentState
from persona_agent.infrastructure.storage import SQLiteStorage
from persona_agent.memory.models import ActivityMemoryContext, MemoryRecord, MemorySettings
from persona_agent.memory.persistence import MemoryPersistence
from persona_agent.memory.retrieval import (RECALL_REFILL_PAGES, activity_detail_query,
    activity_display_key, pick_for_display, query_kind)
from persona_agent.persona.needs import NeedState

TIME=datetime(2026,11,1,10,tzinfo=timezone.utc)
SCOPE="qq-1:FriendMessage:alice"
AGENT="persona-001"


def memory(identifier,activity_id="a",*,phase="stage",text=None,checkpoint=None,**changes):
    if phase=="stage":
        checkpoint=checkpoint or identifier
    if text is None:
        text=("片段内容 "+identifier if phase=="stage" else
              "活动“模拟活动”执行失败。" if phase=="failed" else "活动“模拟活动”完成。")
    draft=dict(kind="story",visibility="scope",scope_id=SCOPE,speaker_id=None,about_id="agent:"+AGENT,
        claim_type="event",occurred_at=TIME,text=text,source_event_id="feedback:"+identifier,provenance="simulated",
        topic_key="intentionally-same-topic",memory_id="memory:"+identifier,agent_id=AGENT,revision=1,
        created_at=TIME,updated_at=TIME,activity=ActivityMemoryContext(activity_id=activity_id,definition_id="watch",
            name="模拟活动",phase=phase,checkpoint_id=checkpoint,
            task="partial" if phase=="stage" else "failure" if phase=="failed" else "success"))
    return MemoryRecord(**{**draft,**changes})


class ActivityDisplayTests(unittest.TestCase):
    def test_nonadjacent_stages_collapse_before_limit_and_do_not_modify_records(self):
        first=memory("one")
        second=memory("two")
        other=memory("other",activity_id="b")
        before=tuple(record.model_dump_json() for record in (first,second,other))
        self.assertEqual(pick_for_display(((first,3),(other,2),(second,1)),3),(first,other))
        self.assertEqual(tuple(record.model_dump_json() for record in (first,second,other)),before)

    def test_same_topic_does_not_collapse_different_activity_instances(self):
        first=memory("one",activity_id="a")
        other=memory("two",activity_id="b")
        self.assertEqual(first.topic_key,other.topic_key)
        self.assertEqual(pick_for_display(((first,2),(other,1)),3),(first,other))

    def test_same_activity_id_is_grouped_even_if_auxiliary_topic_keys_differ(self):
        first=memory("one",topic_key="topic:one")
        other=memory("two",topic_key="topic:two")
        self.assertEqual(pick_for_display(((first,2),(other,1)),3),(first,))

    def test_grouping_respects_actor_scope_provenance_kind_and_definition(self):
        first=memory("one")
        for changes in ({"scope_id":"other"},{"visibility":"self","scope_id":None},
                        {"provenance":"inferred"},{"kind":"episodic"},
                        {"agent_id":"other","about_id":"agent:other"},
                        {"activity":ActivityMemoryContext(**{**first.activity.model_dump(),"definition_id":"other"})}):
            with self.subTest(changes=changes):
                other=MemoryRecord(**{**first.model_dump(),"memory_id":"memory:other",**changes})
                self.assertEqual(pick_for_display(((first,2),(other,1)),3),(first,other))

    def test_missing_activity_or_subject_is_not_guessed_from_topic(self):
        for changes in ({"activity":None},{"about_id":None},{"speaker_id":"someone"}):
            with self.subTest(changes=changes):
                first=memory("one",**changes)
                other=memory("two",**changes)
                for field,value in changes.items():
                    self.assertEqual(getattr(first,field),value)
                    self.assertEqual(getattr(other,field),value)
                self.assertIsNone(activity_display_key(first))
                self.assertEqual(pick_for_display(((first,2),(other,1)),3),(first,other))

    def test_meaningful_whole_summary_can_represent_stages(self):
        stage=memory("stage",text="某个阶段很有趣")
        summary=memory("whole",phase="completed",checkpoint="whole",text="整项观看结束，记录了两个主要内容")
        self.assertEqual(pick_for_display(((stage,3),(summary,2)),3),(summary,))

    def test_bare_completion_does_not_hide_informative_stage(self):
        bare=memory("end",phase="completed")
        stage=memory("stage",text="看到了火星探索的片段")
        self.assertEqual(pick_for_display(((bare,3),(stage,2)),3),(stage,))

    def test_keyword_match_beats_whole_summary_that_does_not_answer_query(self):
        summary=memory("whole",phase="completed",checkpoint="whole",text="整项活动结束，有几段有趣内容")
        stage=memory("stage",text="火星探索")
        self.assertEqual(pick_for_display(((summary,3),(stage,2)),3,query="火星探索"),(stage,))

    def test_representative_keeps_its_own_position_in_rank_order(self):
        stage=memory("stage")
        other=memory("other",activity_id="b")
        summary=memory("whole",phase="completed",checkpoint="whole",text="整项内容总结")
        self.assertEqual(pick_for_display(((stage,3),(other,2),(summary,1)),3),(other,summary))

    def test_detail_query_allows_two_stages_per_activity_with_global_limit(self):
        stages=tuple(memory(str(i)) for i in range(3))
        other=memory("other",activity_id="b")
        ranked=tuple((record,4-i) for i,record in enumerate((*stages,other)))
        self.assertEqual(pick_for_display(ranked,3,query="讲讲活动各阶段发生了什么"),(*stages[:2],other))
        self.assertEqual(pick_for_display(ranked,1,query="讲讲活动各阶段发生了什么"),(stages[0],))

    def test_detail_routing_does_not_override_user_plans_or_user_history(self):
        for text in ("讲讲活动过程","各阶段的内容","说说过程"):
            self.assertTrue(activity_detail_query(text))
            self.assertEqual(query_kind(text),"experience")
        for text in ("我的活动过程","我的未来计划详细过程","他刚才的活动过程"):
            self.assertFalse(activity_detail_query(text))


class ActivityRecallRefillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=TemporaryDirectory()
        self.settings=MemorySettings(candidate_limit=2,recall_limit=3)
        self.store=PersonaStore(SQLiteStorage(Path(self.directory.name)/"recall.sqlite3"),memory=self.settings)
        await self.store.open()
        self.state=AgentState(agent_id=AGENT,updated_at=TIME,
            needs=NeedState(energy=40,satiety=70,boredom=30,social_need=50))
        await self.store.create_state(self.state)
        definition=ActivityDefinition(definition_id="watch",name="模拟观看",supported_modes=["simulated"],
            estimated_duration_seconds=1800)
        executor=SimulatedActivityExecutor()
        for identifier in ("a","b","c"):
            instance=create_activity_instance(definition,activity_id=identifier,agent_id=AGENT,
                mode=ExecutionMode.SIMULATED,created_at=TIME)
            await self.store.create_activity(definition,instance)
            context=ActivityContext(definition=definition,instance=instance,now=TIME)
            await self.store.commit_activity_result("start",context,await executor.start(context))

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def report(self,activity,number,seconds,scope=SCOPE):
        return await self.store.report_activity_feedback(AGENT,activity,
            ActivityFeedback(checkpoint_id=f"{activity}-{number}",task="partial",summary=f"片段内容 {number:03}"),
            now=TIME+timedelta(seconds=seconds),scope_id=scope)

    async def populate(self):
        await self.report("b",1,1)
        await self.report("c",2,2)
        for number in range(15):
            await self.report("a",number,100+number)

    async def recall(self,query="你刚才做了什么"):
        async with self.store._repositories() as (core,_):
            return await MemoryPersistence(core).recall(AGENT,SCOPE,self.settings,query=query,
                now=TIME+timedelta(hours=1))

    async def test_recent_experience_refills_after_fifteen_stages_of_one_activity(self):
        await self.populate()
        picked=await self.recall()
        self.assertEqual(len(picked),3)
        self.assertEqual({record.activity.activity_id for record in picked},{"a","b","c"})

    async def test_keyword_refill_can_find_other_activities_beyond_first_term_page(self):
        await self.populate()
        picked=await self.recall("片段")
        self.assertEqual(len(picked),3)
        self.assertEqual({record.activity.activity_id for record in picked},{"a","b","c"})

    async def test_empty_query_refills_current_scope_with_distinct_activities(self):
        await self.populate()
        picked=await self.recall("")
        self.assertEqual({record.activity.activity_id for record in picked},{"a","b","c"})

    async def test_detail_query_is_still_bounded_per_activity(self):
        await self.populate()
        picked=await self.recall("讲讲活动各阶段发生了什么")
        self.assertEqual(len(picked),3)
        self.assertEqual(sum(record.activity.activity_id=="a" for record in picked),2)
        self.assertTrue(any(record.activity.activity_id!="a" for record in picked))

    async def test_refill_is_read_only_and_keeps_all_checkpoint_records(self):
        await self.populate()
        tables=("agent_events","state_settlements","memories","memory_revisions","memory_sources","memory_receipts","memory_terms")
        async with self.store._repositories() as (core,_):
            before={table:await core.session.fetch_all(table,limit=1000) for table in tables}
        self.assertEqual(len(before["memories"]),17)
        await self.recall()
        await self.recall("片段")
        self.assertEqual(await self.store.get_state(AGENT),self.state)
        async with self.store._repositories() as (core,_):
            self.assertEqual({table:await core.session.fetch_all(table,limit=1000) for table in tables},before)
            stored=await MemoryPersistence(core).list(AGENT,SCOPE,limit=100)
        self.assertEqual(len(stored),17)

    async def test_refill_has_global_page_budget_and_strictly_advancing_cursors(self):
        self.settings=MemorySettings(candidate_limit=1,recall_limit=3)
        for number in range(15):
            await self.report("a",number,100+number)
        async with self.store._repositories() as (core,_):kind=type(core.session)
        original=kind.fetch_all
        calls=[]
        async def checked(session,table,**kwargs):
            self.assertIn(table,("memories","memory_terms"))
            where=kwargs.get("where") or {}
            self.assertEqual(where.get("agent_id"),AGENT)
            self.assertIn(where.get("scope_id"),(SCOPE,""))
            self.assertLessEqual(kwargs.get("limit",100),128 if kwargs.get("where_in") else self.settings.candidate_limit)
            calls.append((table,kwargs))
            return await original(session,table,**kwargs)
        with patch.object(kind,"fetch_all",new=checked):
            picked=await self.recall()
        self.assertEqual(len(picked),1)
        supplemental=[kwargs for _,kwargs in calls if kwargs.get("before")]
        self.assertEqual(len(supplemental),RECALL_REFILL_PAGES)
        cursors=[kwargs["before"]["sort_key"] for kwargs in supplemental]
        self.assertTrue(all(a>b for a,b in zip(cursors,cursors[1:])))

    async def test_scoped_and_self_streams_share_one_refill_budget(self):
        self.settings=MemorySettings(candidate_limit=1,recall_limit=3)
        for number in range(15):
            await self.report("a",number,100+number)
            await self.report("c",number,200+number,scope=None)
        async with self.store._repositories() as (core,_):kind=type(core.session)
        original=kind.fetch_all
        pages=[]
        async def checked(session,table,**kwargs):
            if kwargs.get("before"):
                pages.append((kwargs["where"]["scope_id"],kwargs["before"]["sort_key"]))
            return await original(session,table,**kwargs)
        with patch.object(kind,"fetch_all",new=checked):
            picked=await self.recall()
        self.assertEqual([record.activity.activity_id for record in picked],["a"])
        self.assertEqual(len(pages),RECALL_REFILL_PAGES)
        self.assertEqual({scope for scope,_ in pages},{SCOPE,""})
        for scope in (SCOPE,""):
            cursors=[cursor for current,cursor in pages if current==scope]
            self.assertTrue(all(a>b for a,b in zip(cursors,cursors[1:])))

    async def test_refill_stops_as_soon_as_display_limit_is_satisfied(self):
        await self.populate()
        self.settings=MemorySettings(candidate_limit=2,recall_limit=1)
        async with self.store._repositories() as (core,_):kind=type(core.session)
        original=kind.fetch_all
        async def checked(session,table,**kwargs):
            self.assertFalse(kwargs.get("before"))
            return await original(session,table,**kwargs)
        with patch.object(kind,"fetch_all",new=checked):
            self.assertEqual(len(await self.recall()),1)

    async def test_refill_does_not_cross_scopes_or_publish_unknown_self_summaries(self):
        await self.report("a",1,1)
        await self.report("b",2,2,scope="other")
        await self.report("c",3,3,scope=None)
        for query in ("你刚才做了什么","片段",""):
            picked=await self.recall(query)
            self.assertEqual([record.activity.activity_id for record in picked],["a"])
