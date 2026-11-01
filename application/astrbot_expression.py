"""AstrBot v4.29.3 主动表达适配器；只生成一次纯文本，无工具循环。"""

import json
from ..cognition.prompt_builder import CONTEXT_GUARD


class AstrBotExpressionAdapter:
    def __init__(self,context):self.context=context

    def enabled(self,scope_id):
        if not self.context.get_config(umo=scope_id).get("provider_settings",{}).get("enable",True):
            raise ValueError("会话模型已关闭")
        platforms=self.context.platform_manager.get_insts()
        platform_id=scope_id.split(":",1)[0]
        if not any(p.meta().id==platform_id and p.meta().name=="aiocqhttp" for p in platforms):
            raise ValueError("主动表达当前仅接入 aiocqhttp 私聊")

    async def generate(self,intent,prompt):
        scope=intent.target.scope_id;self.enabled(scope)
        manager=self.context.conversation_manager
        cid=await manager.get_curr_conversation_id(scope)
        if not cid:raise ValueError("目标没有现有 AstrBot 对话")
        conv=await manager.get_conversation(scope,cid)
        if conv is None:raise ValueError("目标对话已删除")
        _,persona,_,_=await self.context.persona_manager.resolve_selected_persona(
            umo=scope,conversation_persona_id=conv.persona_id,platform_name="aiocqhttp",
            provider_settings=self.context.get_config(umo=scope).get("provider_settings",{}))
        history=json.loads(conv.history) if len(conv.history)<=65536 else []
        if not isinstance(history,list):raise ValueError("对话历史格式错误")
        # 保留少量既有自然语言上下文，不携带工具调用和历史系统指令。
        contexts=[{"role":m["role"],"content":m["content"][:1500]} for m in history[-8:]
            if isinstance(m,dict) and m.get("role") in ("user","assistant") and isinstance(m.get("content"),str)]
        provider=await self.context.get_current_chat_provider_id(umo=scope)
        response=await self.context.llm_generate(chat_provider_id=provider,prompt=prompt,
            system_prompt=("" if persona is None else persona["prompt"])+"\n"+CONTEXT_GUARD,contexts=contexts)
        if getattr(response,"role","assistant")!="assistant" or getattr(response,"tools_call_name",None):
            raise ValueError("主动表达不执行工具")
        return response.completion_text

    async def send(self,intent):
        self.enabled(intent.target.scope_id)
        from astrbot.api.event import MessageChain
        return await self.context.send_message(intent.target.scope_id,MessageChain().message(intent.body))
