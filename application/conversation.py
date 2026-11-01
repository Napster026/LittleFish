"""QQ 绑定范围到会话占用策略的适配；不依赖 AstrBot。"""


def conversation_scope_allowed(item, settings):
    if settings.qq_platform_ids and item.platform_id not in settings.qq_platform_ids:
        return False
    if item.scope_id in (f"{item.platform_id}:FriendMessage:{user}" for user in settings.qq_private_ids):
        return item.scope_id==f"{item.platform_id}:FriendMessage:{item.user_id}"
    return (not settings.conversation.private_only
        and item.scope_id in (f"{item.platform_id}:GroupMessage:{group}" for group in settings.qq_group_ids))
