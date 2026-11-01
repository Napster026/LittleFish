"""活动已提交事实到经历记忆的适配；不改变评价、活动状态或完成收益。"""

from ..activity.feedback import STAGE_EVENT_TYPE, parse_activity_feedback
from ..activity.models import ActivityStatus, ExecutionMode
from ..memory.models import ActivityMemoryContext
from ..memory.persistence import MemoryPersistence


async def record_activity_memory(core,record,event,settings,*,is_new):
    if event.event_type not in (STAGE_EVENT_TYPE,"ACTIVITY.completed","ACTIVITY.failed"):
        return None
    feedback=parse_activity_feedback(record,event)
    if event.event_type==STAGE_EVENT_TYPE:
        if feedback is None:
            return None
        phase="stage"
    else:
        phase="completed" if event.event_type=="ACTIVITY.completed" else "failed"
        expected=ActivityStatus.COMPLETED if phase=="completed" else ActivityStatus.FAILED
        if (record.instance.status!=expected or event.activity_id!=record.instance.activity_id
                or event.payload.get("agent_id")!=record.instance.agent_id
                or event.payload.get("definition_id")!=record.definition.definition_id
                or event.payload.get("mode")!=record.instance.mode.value):
            raise ValueError("终态经历与已提交的活动事实不一致")
        if record.instance.mode==ExecutionMode.SIMULATED and event.provenance.value!="simulated":
            raise ValueError("模拟经历不能提升为真实观察")
    name=record.definition.name
    name=name if len(name)<=80 else name[:79]+"…"
    if feedback is not None:
        text=feedback.summary
        task=feedback.task or {"success":"success","failure":"failure"}.get(feedback.outcome,"neutral")
        importance=(feedback.experience.importance if feedback.experience is not None and not feedback.experience.uncertain else .5)
        checkpoint=feedback.checkpoint_id
    else:
        text=f"活动“{name}”完成。" if phase=="completed" else f"活动“{name}”执行失败。"
        task="success" if phase=="completed" else "failure"
        importance=.5 if phase=="completed" else .6
        checkpoint=None
    details=ActivityMemoryContext(activity_id=record.instance.activity_id,definition_id=record.definition.definition_id,
        name=name,checkpoint_id=checkpoint,phase=phase,task=task)
    return await MemoryPersistence(core).write_experience(record.instance.agent_id,event,details,text,importance,settings,
                                                         allow_capture=is_new)
