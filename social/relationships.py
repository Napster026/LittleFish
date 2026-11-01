"""纯关系更新：独立冷却与每日绝对变化预算，无时钟或存储副作用。"""

from datetime import date, datetime, timedelta, timezone
import math
from .models import RelationshipProfile


def affinity_day(now,settings):
    zone=timezone(timedelta(minutes=settings.utc_offset_minutes))
    return (now.astimezone(zone)-timedelta(hours=settings.affinity_settlement_hour)).date()


def regress_affinity(value,days,settings):
    """折叠多个自然日；负值不越过零，正向低于保留基线时不下降。"""
    if type(days) is not int or days<0:
        raise ValueError("关系回归天数必须为非负整数")
    if not days or not settings.enabled or not settings.affinity_regression_enabled:
        return value
    ratio=settings.affinity_daily_retention
    step=settings.affinity_negative_recovery
    floor=settings.affinity_positive_baseline
    if value<0:
        magnitude=-value
        if ratio==0:
            return 0.0
        if step==0:
            magnitude*=ratio**days
        elif ratio==1:
            magnitude=max(0,magnitude-step*days)
        else:
            # 大幅负向先按比例快速回归，进入阈值后按最小恢复量回零。
            threshold=step/(1-ratio)
            geometric=0 if magnitude<=threshold else min(days,max(0,math.ceil(
                (math.log(threshold)-math.log(magnitude))/math.log(ratio))))
            magnitude*=ratio**geometric
            magnitude=max(0,magnitude-step*(days-geometric))
        return min(0.0,round(-magnitude,10))
    if value>floor:
        return max(floor,round(floor+(value-floor)*ratio**days,10))
    return value


def regress_relationship(profile,*,now,settings):
    """只生成新档案候选，游标每天推进一次；最近互动、冷却、预算不变。"""
    if not isinstance(now,datetime) or now.utcoffset() is None:
        raise ValueError("关系回归时间必须带时区")
    if not settings.enabled or not settings.affinity_regression_enabled or now<profile.updated_at:
        return profile,None,0
    target=affinity_day(now,settings)
    anchor=date.fromisoformat(profile.affinity_day) if profile.affinity_day else affinity_day(profile.updated_at,settings)
    if target<anchor or (profile.affinity_day is not None and target==anchor):
        return profile,anchor.isoformat(),0
    days=(target-anchor).days
    candidate=RelationshipProfile(**{**profile.model_dump(),"affinity":regress_affinity(profile.affinity,days,settings),
        "affinity_day":target.isoformat(),"updated_at":now,"revision":profile.revision+1})
    return candidate,anchor.isoformat(),days


def advance_relationship(profile,*,agent_id,platform_id,user_id,occurred_at,now,signal,settings):
    if (any(not isinstance(value,datetime) or value.utcoffset() is None for value in (occurred_at,now))
            or occurred_at>now):
        raise ValueError("关系处理需要有效时间且不能早于互动")
    if profile is not None:
        if (profile.agent_id,profile.platform_id,profile.user_id)!=(agent_id,platform_id,user_id):
            raise ValueError("关系更新身份不一致")
        if now<profile.updated_at or occurred_at<profile.last_interaction_at:
            return profile,"out_of_order"
    day=now.astimezone(timezone(timedelta(minutes=settings.utc_offset_minutes))).date().isoformat()
    before=profile or RelationshipProfile(agent_id=agent_id,platform_id=platform_id,user_id=user_id,
        last_interaction_at=occurred_at,budget_day=day,affinity_day=affinity_day(now,settings).isoformat(),revision=1,updated_at=now)
    data=before.model_dump()
    if not settings.affinity_regression_enabled:
        data["affinity_day"]=affinity_day(now,settings).isoformat()  # 停用期间的新互动不在重启后被追溯成更早的倾向。
    if day>before.budget_day:
        data.update(budget_day=day,familiarity_used=0,affinity_used=0)
    data.update(last_interaction_at=occurred_at,updated_at=now)
    if before.cooldown_until is not None and now<before.cooldown_until:
        reason="cooldown"
    else:
        familiarity=min(settings.familiarity_step,max(0,settings.daily_familiarity_limit-data["familiarity_used"]),
                        100-before.familiarity)
        proposed=(settings.positive_affinity_step if signal.polarity=="positive" else
                  -settings.negative_affinity_step if signal.polarity=="negative" else 0)
        room=100-before.affinity if proposed>=0 else 100+before.affinity
        magnitude=min(abs(proposed),max(0,settings.daily_affinity_limit-data["affinity_used"]),room)
        affinity=magnitude if proposed>=0 else -magnitude
        data.update(familiarity=round(before.familiarity+familiarity,10),affinity=round(before.affinity+affinity,10),
            familiarity_used=round(data["familiarity_used"]+familiarity,10),
            affinity_used=round(data["affinity_used"]+abs(affinity),10),
            cooldown_until=now+timedelta(seconds=settings.cooldown_seconds))
        if familiarity or affinity:
            reason="updated"
        elif data["familiarity_used"]>=settings.daily_familiarity_limit or (proposed and data["affinity_used"]>=settings.daily_affinity_limit):
            reason="daily_limit"
        else:
            reason="at_boundary"
    if profile is not None and data==profile.model_dump():
        return profile,reason
    data["revision"]=1 if profile is None else profile.revision+1
    return RelationshipProfile(**data),reason
