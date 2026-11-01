"""明确第一人称计划/偏好的轻量提取；含引用、假设或疑问时保守跳过。"""

import re
from datetime import date, datetime, time, timedelta, timezone
from .models import MemoryDraft
from .retrieval import normalized, search_terms

_TIME = r"大后天|后天|明天|今天|\d{4}-\d{1,2}-\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日|下周[一二三四五六日天]"
_UNCERTAIN = r"[?？\"“”]|如果|假如|可能|也许|不一定|举例|转发|他说|她说|别人说|开玩笑|是否|什么|怎么|吗|要不要|能不能|不知道|是不是|会不会|有没有|能否"


def actor_id(event):
    sender = event.payload.get("sender_id")
    platform = event.payload.get("platform_id")
    if not isinstance(sender,str) or not sender or not isinstance(platform,str) or not platform:
        return None
    return f"{platform}:user:{sender}"


def target_range(text, at, offset_minutes):
    match = re.search(_TIME, text)
    if match is None:
        return None
    value=match.group()
    zone=timezone(timedelta(minutes=offset_minutes))
    try:
        today=at.astimezone(zone).date()
    except (ValueError,OverflowError):
        return None
    try:
        if value in ("今天","明天","后天","大后天"):
            day=today+timedelta(days={"今天":0,"明天":1,"后天":2,"大后天":3}[value])
        elif value.startswith("下周"):
            weekday="一二三四五六日".index(value[-1].replace("天","日"))
            day=today+timedelta(days=7-today.weekday()+weekday)
        else:
            y,m,d=map(int,re.findall(r"\d+",value))
            day=date(y,m,d)
    except (ValueError,OverflowError):
        return None
    try:
        start=datetime.combine(day,time(),tzinfo=zone)
        return start.astimezone(timezone.utc),(start+timedelta(days=1)).astimezone(timezone.utc),match
    except (ValueError,OverflowError):
        return None


def _plan(raw, base, settings):
    when=target_range(raw,base["occurred_at"],settings.utc_offset_minutes)
    if when is None:
        return None
    start,end,match=when
    prefix=raw[:match.start()]
    if not re.fullmatch(r"(?:记住[：:，, ]*)?(?:我(?:现在|以后)?(?:计划|打算|准备|决定|会|要|还是|改成|改到|改为)*|改成|改为|改到|取消|还是改成|)",prefix):
        return None
    body=raw[match.end():].strip(" ，,。！!")
    body=re.sub(r"^(?:会|要|必须|准备|打算|计划|决定|改成|改为|改到)+", "", body)
    negative=bool(re.match(r"^(?:不再|不|取消)",body)) or raw.startswith("取消")
    completed=bool(re.match(r"^(?:已经|已)",body)) and bool(re.search(r"了|完成",body))
    action=re.sub(r"^(?:不再|不会|不要|不|取消|已经|已)","",body)
    action=re.sub(r"(?:了|啦|吧|呢|啊|的)$","",action)
    starts_with_go=action.startswith("去")
    action=re.sub(r"^去", "", action)
    if not 2<=len(action)<=160 or re.search(r"不|没|或者|还是|或是|但|改|只|然后|告诉|说|认为|你|他|她|，|,|；|;|。",action):
        return None
    if not starts_with_go and not re.match(r"^(?:出门|外出|出去|逛街|看|参加|上班|开会|吃|做|交|买|见|运动|学习|休息|旅行|旅游|打|写|读|回|陪|玩)",action):
        return None
    generic=action in ("出门","外出","出去")
    outing=generic or starts_with_go or action.startswith(("逛街","出门","外出","出去"))
    detail="outing" if generic else normalized(action)
    if not detail:
        return None
    correction=bool(re.match(r"^(?:改成|改为|改到|还是改成)",raw)) or bool(re.search(r"改成|改到|改为",prefix))
    hint="cancel" if negative else "complete" if completed else "correct" if correction else "assert"
    if completed and start>base["occurred_at"]:
        return None
    return MemoryDraft(**base,claim_type="plan",target_start_at=start,target_end_at=end,
        time_precision="day",plan_status="cancelled" if negative else "completed" if completed else "planned",
        topic_key="plan:outing" if outing else "plan:"+detail,detail_key=detail,update_hint=hint)


def extract_memory(event, settings, *, text=None, explicit=False):
    raw=event.payload.get("text") if text is None else text
    speaker=actor_id(event)
    if (not isinstance(raw,str) or not raw.strip() or len(raw)>600 or speaker is None
            or event.scope_id is None or event.payload.get("text_truncated")):
        return None
    raw=raw.strip()
    base=dict(scope_id=event.scope_id,speaker_id=speaker,about_id=speaker,
              occurred_at=event.timestamp,source_event_id=event.event_id,provenance=event.provenance,
              text=raw,keywords=search_terms(raw)[-4:],valid_from=event.timestamp)
    if not re.search(_UNCERTAIN,raw) and not re.search(r"[\r\n]",raw):
        first=re.match(r"^(?:记住[：:，, ]*)?我(?!们)",raw)
        implicit=explicit and re.match("^(?:"+_TIME+")",raw)
        correction=re.match(r"^(?:改成|改为|改到|取消|还是改成)",raw)
        if first or correction or implicit:
            plan=_plan(raw,base,settings)
            if plan is not None:
                return plan
            pref=re.fullmatch(r"我(现在|其实|以后)?(不喜欢|不爱|讨厌|喜欢|不吃|爱吃)([^，,。！？!?；;]{1,80})[。！!]?",raw)
            if pref is not None:
                modifier,verb,obj=pref.groups()
                if normalized(obj) and not re.search(r"但|或者|他说|她说|你|他|她",obj):
                    return MemoryDraft(**base,claim_type="preference",
                        topic_key="preference:"+("consume:" if verb in ("不吃","爱吃") else "liking:")+normalized(obj),
                        detail_key=("negative:" if verb in ("不喜欢","不爱","讨厌","不吃") else "positive:")+normalized(obj),
                        update_hint="correct" if modifier else "assert")
    if explicit:
        # 手动正文始终可保存；不能结构化识别时不猜测人物和日期。
        base["about_id"]=None
        return MemoryDraft(**base,claim_type="fact")
    return None
