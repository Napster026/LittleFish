"""既有聊天分类到关系信号及管理员显示；不重跑评价或调用模型。"""

from ..social.models import RelationshipSignal


def relationship_signal(result,settings):
    polarity="neutral"
    if result.reason=="matched_phrase":
        if result.rule_id in settings.positive_rule_ids:polarity="positive"
        elif result.rule_id in settings.negative_rule_ids:polarity="negative"
    return RelationshipSignal(polarity=polarity,rule_id=result.rule_id)


REASONS={"updated":"已更新","cooldown":"关系冷却","daily_limit":"达到每日上限","at_boundary":"无可增加幅度",
    "out_of_order":"旧消息不改写关系","disabled":"关系功能关闭","private_only":"仅私聊","not_directed":"未指向角色",
    "empty_message":"空消息","command":"指令不计互动","unknown_identity":"身份不完整","self_message":"机器人自身消息",
    "daily_regression":"每日倾向回归","expired":"来源过期","history_not_backfilled":"历史消息不补算"}


def format_relationship(profile,changes,settings,*,history=False):
    lines=[]
    if not history:
        if profile is None:
            lines.append("尚无当前用户的关系档案")
        else:
            lines.extend((f"当前用户：{profile.platform_id} / {profile.user_id}",
                f"熟悉度：{profile.familiarity:.1f} / 100；互动倾向：{profile.affinity:+.3f}（-100..100）",
                f"最近互动：{profile.last_interaction_at.isoformat()}；关系修订：{profile.revision}",
                f"倾向结算日：{profile.affinity_day or '尚未建立'}；每日 {settings.affinity_settlement_hour:02}:00（{'启用' if settings.affinity_regression_enabled else '关闭'}）",
                f"预算日：{profile.budget_day}；日累计熟悉度 {profile.familiarity_used:.2f}/{settings.daily_familiarity_limit:g}，互动倾向绝对变化 {profile.affinity_used:.2f}/{settings.daily_affinity_limit:g}",
                "关系是互动估计，不代表信任、承诺或权限。"))
    if not changes:
        lines.append("当前会话暂无关系变化回执")
    for item in changes:
        period=f"（{item.from_day}→{item.to_day}，{item.days} 天）" if item.kind=="regression" else ""
        lines.append(f"{item.at.isoformat()}：{REASONS[item.reason]}{period}；熟悉度 {item.familiarity_delta:+.2f}，互动倾向 {item.affinity_delta:+.3f}；规则 {item.rule_id or '无'}；来源 {item.source_event_id}")
    return "\n".join(lines)
