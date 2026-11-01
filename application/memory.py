"""记忆管理结果显示；检索与归并规则属于 memory 包。"""

from datetime import timedelta, timezone
from ..memory.models import MemoryRecord


def memory_summary(record, settings, *, short=False):
    text=record.text if not short or len(record.text)<=90 else record.text[:89]+"…"
    target=""
    if record.target_start_at is not None:
        zone=timezone(timedelta(minutes=settings.utc_offset_minutes))
        target=f"；目标日期 {record.target_start_at.astimezone(zone).date()}；{record.plan_status}"
    return f"{record.memory_id}（修订 {record.revision}，{record.claim_type}{target}）\n{text}"


def format_memory_result(action, result, settings):
    if action in ("add","delete"):
        names={"created":"已新增","duplicate":"已关联重复陈述","enriched":"已补充",
               "corrected":"已更正","cancelled":"已标记取消","completed":"已标记完成",
               "ignored":"未生成记忆（自动提取仅支持明确陈述；手动正文最多 600 字符）","deleted":"已删除"}
        detail="" if result.memory_id is None else f"\n{result.memory_id}"
        if result.revision is not None:
            detail+=f"；修订 {result.revision}"
        return names[result.action]+detail
    if action=="pending":
        if not result:
            return "当前会话没有待修复记忆"
        text="\n".join(f"{row['memory_id']}（修订 {row['revision']}；索引待修复）" for row in result)
        text+="\n这些记录暂不参与召回；保留原数据，下次加载重试。可用 /persona_memory delete ID 删除指定记录。"
        if len(result)==10:
            text+=f"\n继续查看：/persona_memory pending {result[-1]['memory_id']}"
        return text
    if action=="show":
        if result is None:
            return "当前会话没有这条记忆"
        return memory_summary(result,settings)+f"\n来源：{result.source_event_id}（{result.provenance.value}）\n陈述时间：{result.occurred_at.isoformat()}"
    if action=="history":
        return "\n\n".join(f"{row['action']}；来源 {row['source_event_id']}\n"+
            memory_summary(MemoryRecord.model_validate_json(row["record_json"]),settings,short=True) for row in result) or "暂无修订"
    text="\n\n".join(memory_summary(item,settings,short=True) for item in result)
    if action=="list" and len(result)==10:
        text+=f"\n\n继续查看：/persona_memory list {result[-1].memory_id}"
    return text or "当前会话没有匹配记忆"
