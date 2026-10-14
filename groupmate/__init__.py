# -*- coding: utf-8 -*-
"""AI 群友核心逻辑。

分层:
  config   —— 配置(人设/触发/护栏/模型)
  message  —— 统一的群消息模型
  policy   —— 决策引擎:这条要不要回(可离线测试)
  llm      —— 大模型调用(OpenAI 兼容)
  backend  —— 微信收发后端(实现见 wx_backend.py / mock_backend.py)
  bot      —— 串起监听 → 决策 → 生成 → 发送 的主循环
"""
from .config import BotConfig, GuardConfig, LLMConfig, PersonaConfig, TriggerConfig
from .message import GroupMessage
from .policy import Decision, ReplyPolicy

__all__ = [
    "BotConfig", "TriggerConfig", "GuardConfig", "LLMConfig", "PersonaConfig",
    "GroupMessage", "ReplyPolicy", "Decision",
]

__version__ = "0.1.0"
