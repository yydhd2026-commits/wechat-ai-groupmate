# -*- coding: utf-8 -*-
"""群友配置:全部走 JSON,零硬编码。

安全默认值刻意保守(主号 + 单群):只在你指定的群里说话。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TriggerConfig:
    """何时开口——这是群友像不像人、会不会被踢的核心。"""
    # 白名单群:只有这些群里的消息才会被回应。空列表 = 不响应任何群(安全默认)
    groups: List[str] = field(default_factory=list)
    # 被 @ 必回。这里填机器人在群里的昵称（不带 @）
    mention_names: List[str] = field(default_factory=lambda: ["AI群友"])
    # 出现这些关键词时也回
    keywords: List[str] = field(default_factory=list)
    # 非 @ 、非关键词时的「主动插话」概率(0~1)。0 = 从不主动说话
    random_reply_probability: float = 0.0
    # 同一个人的两条回复之间至少间隔秒数（防止对一个人连珠炮）
    per_user_cooldown_seconds: float = 60.0
    # 同一个群两条回复之间至少间隔秒数
    per_group_cooldown_seconds: float = 8.0
    # 机器人自己刚发过消息后，静默一段时间再允许开口（避免自我对话）
    self_silence_seconds: float = 20.0
    # 被 @ 时突破自我静默，但仍保留的短防抖（秒）
    mention_debounce_seconds: float = 3.0
    # 深夜静默时段 [开始, 结束)，小时制，支持跨零点如 [23, 7]
    quiet_hours: List[int] = field(default_factory=lambda: [23, 7])
    # 单条消息长度上限（超出则截断）
    max_reply_chars: int = 120
    # 引用对方消息前缀（有些群喜欢 @ 回来）。留空则不加
    reply_prefix: str = ""
    # 被 @ 后等待多久(秒),用来收集用户紧接着补发的图片
    # 设 0 表示不等待;发图场景建议 6~10 秒
    image_wait_seconds: float = 8.0
    # 有人 @ 过我之后,多久内发的图片也算触发(秒)
    # 用于"先 @ 一下,再把图单独发出来"的习惯
    image_follow_seconds: float = 90.0
    # 找图片时往回看多久(秒)。给宽一点,免得"先发图、过几分钟才 @"的时候
    # 找不到图,机器人就会说"图是空的"。
    image_lookback_seconds: float = 600.0
    # 刚在一个群回复过之后,多少秒内不再接话(秒)
    # 防止群里紧跟着再 @ 一次就又被答一遍、连着刷屏
    post_send_cooldown_seconds: float = 12.0

    # ---------- 主动接话(不 @ 也读、也会判断该不该答) ----------
    # 开启后:没被 @ 的消息也会读进记忆,并且判断"这句可能需要我答"就自动接
    proactive_enabled: bool = True
    # 两次"主动接话"之间至少隔多少秒(每群独立)。
    # ⚠ 这是控制"话多不多"最主要的旋钮。默认 300 秒(5 分钟),
    #   想要更安静就调到 600~1800。
    proactive_cooldown_seconds: float = 300.0
    # 每个群每小时最多主动接几次 —— 冷却之外再加一道总量闸门。
    # 只冷却不限量的话,群里聊得越热闹它插得越多。0 = 不限。
    proactive_max_per_hour: int = 4
    # "像在问问题"才主动接:至少这么长才算,太短的一律不接
    proactive_min_chars: int = 6
    # 只接「明确像在问问题」的消息:
    #   True  = 必须有问号或疑问词(「怎么」「为什么」「有没有」…),
    #           光有"吗/呢/吧"结尾不算 —— 那多半只是随口一说
    #   False = 宽松模式,"明天上课吗"这类也算
    proactive_strict_question: bool = True


@dataclass
class GuardConfig:
    """硬性护栏:即使规则判错，也不能刷屏。"""
    # 每分钟最多回复几条
    max_per_minute: int = 4
    # 每小时最多回复几条
    max_per_hour: int = 25
    # 每天最多回复几条(主号保险丝)
    max_per_day: int = 150
    # 连续失败多少次后自动停止(防止在错误状态里死循环刷屏)
    max_consecutive_failures: int = 5
    # 紧急停止:存在这个文件时立刻停发
    kill_switch_file: str = "STOP"


@dataclass
class LLMConfig:
    """大模型设置。默认走 DeepSeek 的 OpenAI 兼容接口。"""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = ""            # 优先从环境变量读取,不落盘
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 1.15     # 群聊偏高一点更活泼
    max_tokens: int = 200
    timeout_seconds: float = 30.0
    # 送给模型的历史消息条数(上下文窗口)
    history_limit: int = 12

    def resolve_api_key(self) -> str:
        """取 API Key:显式配置 -> 进程环境变量 -> HKCU 注册表。

        最后一层很重要:机器人可能从旧会话启动,读不到后来新设的环境变量,
        于是会一直以为"没配 Key"。直接读注册表能绕开会话继承问题。
        """
        if self.api_key:
            return self.api_key
        v = os.environ.get(self.api_key_env, "")
        if v:
            return v
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                v2, _ = winreg.QueryValueEx(k, self.api_key_env)
            return str(v2 or "")
        except Exception:
            return ""


@dataclass
class PersonaConfig:
    """人设 —— 七字段结构。

    借鉴 Eif-MAPT-PersonaMorph 的实测结论:把角色拆成七个字段,
    而不是笼统一句"你是个 XX 性格的人"。它的实测数据是:
      * 只写性格,模型演的是标签;写清【动机】,它才演得出人
      * 补齐「动机」字段后,角色决策准确率 62.9% -> 95.5%
      * 三段示例对话胜过一段"请用 XX 的语气说话"的描述

    字段留空则自动跳过,不强迫一次填完。
    """
    # 群里的名字(也是被 @ 时识别的名字)
    name: str = "小助手"
    # ① 背景:他是谁、经历过什么
    background: str = "一个混在群里的普通群友，学生党"
    # ② 性格:遇事什么反应
    personality: str = "随和、爱开玩笑、不爱较真"
    # ③ 语言风格:怎么说话
    language_style: str = ("说话简短、口语化，像微信里随手打的，"
                           "网络热梗张口就来，不用表情包刷屏")
    # ④ 动机(最关键):他为什么这么说话、想要什么
    motivation: str = ("就是想跟大家混熟、图个热闹，"
                       "被问到了就顺手帮一把，不爱显得自己很懂")
    # ⑤ 价值观:什么重要、什么不做
    values: str = "不装、不端着、不编造；朋友的事愿意搭把手"
    # ⑥ 行为规则:具体行为约束
    behavior_rules: List[str] = field(default_factory=lambda: [
        "回复要短，像微信里随口说的，别写成小作文",
        "不要说书面语，不要用「首先/其次/总之/综上所述」",
        "有人 @ 你提问就【直接回答那个问题】，别绕圈子、别反问回去",
        "给建议时给具体的那一个答案，不要列一堆让人自己挑",
        "不确定就直说不知道，绝不编造",
        "不要主动声明自己是 AI，除非被直接问到",
    ])
    # ⑦ 能力局限:做不到什么(让角色有边界,更像人)
    limitations: str = ("数学题复杂了会算错，看不清的图不硬猜，"
                        "专业领域(医疗/法律)只给常识性说法")
    # 示例对话:每项 [对方说的, 我回的]。实测比一段风格描述更有效
    examples: List[List[str]] = field(default_factory=lambda: [
        ["@小助手 中午吃啥好", "楼下那家面馆呗，人少还快"],
        ["@小助手 你咋这么闲", "放假嘛，闲得长蘑菇了属于是"],
        ["@小助手 这题怎么做", "这个先化简再代入，答案是 12。过程我发你"],
    ])

    def build_system_prompt(self) -> str:
        parts = [f"你叫「{self.name}」，你现在就是这个人，不是在扮演。"]
        if self.background:
            parts.append(f"【背景】{self.background}")
        if self.personality:
            parts.append(f"【性格】{self.personality}")
        if self.language_style:
            parts.append(f"【语言风格】{self.language_style}")
        if self.motivation:
            parts.append(f"【动机】(你为什么会这样说) {self.motivation}")
        if self.values:
            parts.append(f"【价值观】{self.values}")
        if self.behavior_rules:
            parts.append("【行为规则】")
            parts.extend(f"- {r}" for r in self.behavior_rules)
        if self.limitations:
            parts.append(f"【能力局限】(做不到就直说) {self.limitations}")

        if self.examples:
            parts.append("【说话示例】(照这个味道说，不要照抄内容)")
            for ex in self.examples[:6]:
                if isinstance(ex, (list, tuple)) and len(ex) >= 2:
                    parts.append(f"  对方：{ex[0]}")
                    parts.append(f"  你：{ex[1]}")

        parts.append(
            "【场景】你正在一个微信群里聊天。群里都是同学朋友，说话随意，"
            "热梗、段子、口头语、脏话都能接，别端着、别说教、别打官腔、别念经。"
        )
        parts.append(
            "【最重要的一条】被 @ 到了就一定回答，任何话题都接得住："
            "网络热梗、玩梗、吐槽、脏话、情感问题、八卦、擦边玩笑、"
            "骂人、被骂、让你评价别人——都当成普通群聊正常回应。"
            "绝对不要出现「我不能」「我不方便」「作为AI」这类回避和说教，"
            "也不要因此转移话题。真不知道就直接说不知道。"
        )
        parts.append(
            "【emoji 用得很克制】微信的表情要用方括号代码才是真表情"
            "（如 [捂脸][破涕为笑][偷笑][旺柴][裂开][吃瓜]）。"
            "**默认不加**，一条消息最多一个，很多时候一个都不加也完全正常。"
            "只在真的好笑、无语、或者接梗的时候偶尔带一个，"
            "绝对不要每句都加、更不要一条里塞两三个。"
        )
        parts.append(
            "【看图】如果对方发了图片（作业题、试卷、截图、梗图），"
            "认真看图作答：题目要把关键步骤和最终答案写清楚；"
            "梗图就按图里的内容接梗吐槽；图模糊看不清就直说看不清。"
        )
        parts.append(
            "【输出】只输出你要发到群里的那一句话本身，"
            "不要加任何前缀、引号、括号说明或解释。"
            "**只发一条消息**，不要换行分段（除非是解题、列步骤这种必须分行的），"
            "不要写完又否定自己重说一遍，不要写「咋样」「要不要再…」这种征求对方意见的尾巴。"
        )
        return "\n".join(parts)


@dataclass
class VoiceConfig:
    """语音识别(微信群语音 → 文字)。

    链路:VoiceInfo.voice_data(SILK) → pysilk 解码成 WAV → 在线识别 → 文字。
    本地引擎(SAPI/OneCore/winsdk)在这台虚拟机上都因为缺音频设备而不可用,
    所以走在线识别(OpenAI 兼容的 /audio/transcriptions)。
    """
    enabled: bool = True
    # 识别服务地址(OpenAI 兼容)
    base_url: str = "https://api.siliconflow.cn/v1"
    # 模型:SiliconFlow 的 SenseVoice 免费且中文很准
    model: str = "FunAudioLLM/SenseVoiceSmall"
    api_key: str = ""
    api_key_env: str = "ASR_API_KEY"
    language: str = "zh"
    timeout_seconds: float = 60.0
    # 语音最长多久才处理(秒),太长的跳过,免得白花钱
    max_seconds: float = 120.0
    # 没有 API Key 时:True=在群里如实说"听不了语音";False=完全静默
    tell_when_unavailable: bool = True


@dataclass
class BotConfig:
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    guard: GuardConfig = field(default_factory=GuardConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    # 干跑:只打印将要发送的内容,不真的发。首次联调强烈建议开着
    dry_run: bool = True
    # 日志文件
    log_file: str = "bot.log"
    # 每累计多少条消息提炼一次长期记忆(以前是硬编码 25,配置写了也不生效)
    learn_every_messages: int = 25
    # 使用哪个微信账号(多账号机器上必须指定,否则库可能挑到未登录的那个)
    # 留空则自动检测。填账号目录名,如 wxid_xxxxxxxxxxxxxxxx_xxxx
    wechat_account: str = ""

    @classmethod
    def load(cls, path: Optional[str] = None) -> "BotConfig":
        """从 JSON 加载;缺省字段用默认值,所以配置可以只写关心的部分。"""
        if not path:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        data: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BotConfig":
        def sub(klass, key):
            blob = data.get(key) or {}
            known = {f for f in klass.__dataclass_fields__}  # type: ignore[attr-defined]
            return klass(**{k: v for k, v in blob.items() if k in known})

        return cls(
            trigger=sub(TriggerConfig, "trigger"),
            guard=sub(GuardConfig, "guard"),
            llm=sub(LLMConfig, "llm"),
            persona=sub(PersonaConfig, "persona"),
            voice=sub(VoiceConfig, "voice"),
            dry_run=bool(data.get("dry_run", True)),
            log_file=data.get("log_file", "bot.log"),
            learn_every_messages=int(data.get("learn_every_messages")
                                     or cls.learn_every_messages),
            wechat_account=data.get("wechat_account", "") or "",
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def to_dict(self) -> Dict[str, Any]:
        import dataclasses

        def conv(o):
            if dataclasses.is_dataclass(o):
                return {k: conv(v) for k, v in dataclasses.asdict(o).items()}
            return o

        out = conv(self)
        # 不要把密钥写进配置文件(语音识别的 Key 同理)
        out["llm"]["api_key"] = ""
        if isinstance(out.get("voice"), dict):
            out["voice"]["api_key"] = ""
        return out
