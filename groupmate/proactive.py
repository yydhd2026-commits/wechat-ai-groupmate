# -*- coding: utf-8 -*-
"""主动接话判断。

要解决的问题:群里聊天时,有些话机器人其实该答(比如在问问题、
或者话题正好接着它刚说的),但没 @ 它,于是它就装死了。

设计原则(宁可少说话,也不能乱插嘴刷屏):
  1. 先过"硬闸门" —— 明显是在跟别人说话、@了别人、纯表情、太短 → 一律不接
  2. 只对"像问题、或者接着机器人的话往下说"的消息才考虑主动开口
  3. 有频率上限 + 冷却,防止变成话痨
  4. 真正的 @ 永远优先,不受这一层限制

判定分两级:
  * 规则级(零成本):快、稳,能挡住绝大多数不该接的话
  * 模型级(可选):让模型读上下文判断"这句需要我答吗",更准但费 token
"""
from __future__ import annotations

import re
import time
from typing import List, Optional, Tuple

# ---------- 复用 message 模块里带边界判定的实现(避免两套逻辑飘) ----------
try:
    from .message import _AT_OTHER as _AT_OTHER_RE
    from .message import _at_someone_else as _shared_at_other
except Exception:                                     # pragma: no cover
    _AT_OTHER_RE = None
    _shared_at_other = None

# ---------- 明显的"不是跟我说"的信号 ----------

#: 消息里 @ 了某个名字(兜底用;正常走 message 模块的共享实现)
_AT_OTHER = re.compile(r"@(?!\s*$)[^\s@]{1,20}") if _AT_OTHER_RE is None \
    else _AT_OTHER_RE

# 疑问信号:问号 或 疑问词
_QUESTION_MARKS = ("?", "？", "吗", "呢", "吧", "么")
_QUESTION_WORDS = (
    "怎么", "咋", "如何", "为什么", "为啥", "为何", "多少", "几个", "哪", "谁",
    "是不是", "有没有", "能不能", "可不可以", "行不行", "好不好", "要不要",
    "有人", "大家", "问一下", "请教", "帮忙", "帮我", "求", "懂", "会吗",
    "什么", "啥", "哪儿", "哪里", "几点", "多久",
)

# 纯情绪/无意义内容 => 不接
_NOISE = re.compile(
    r"^[\s。，、！？~～…\.\,\!\?\+\-=_\W]*$"
)
# 纯表情代码
_EMOJI_ONLY = re.compile(r"^(\[[^\[\]]{1,8}\]|\s)+$")
# 纯复读/附和
_ECHO = {"哈哈", "哈哈哈", "hhhh", "草", "6", "66", "666", "牛", "牛逼", "笑死",
         "确实", "好的", "嗯", "哦", "啊", "额", "emmm", "..."}


def _is_noise(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if len(t) <= 1:
        return True
    if _NOISE.match(t):
        return True
    if _EMOJI_ONLY.match(t):
        return True
    if t.lower() in _ECHO:
        return True
    return False


def _at_someone_else(text: str, my_names: List[str]) -> bool:
    """消息里 @ 了别人(不是 @ 我)。

    直接复用 message 模块的实现,避免两处逻辑不一致。
    """
    if _shared_at_other is not None:
        return _shared_at_other(text, my_names)
    hits = _AT_OTHER.findall(text or "")
    if not hits:
        return False
    mine = {"@" + n for n in (my_names or []) if n}
    for h in hits:
        if h not in mine:
            return True
    return False


def _looks_like_question(text: str) -> bool:
    """看着像在提问吗。

    ⚠ 中文最常见的疑问句是"吗/呢/吧/么"结尾(「明天上课吗」「这题你会做吧」),
    以前只用 _QUESTION_WORDS 找词,这些全漏判 -> 该接的没接。
    现在把 _QUESTION_MARKS 用上(它本来就是为这个定义的,却一直没人引用)。
    """
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t or "？" in t:
        return True
    if any(w in t for w in _QUESTION_WORDS):
        return True
    # 句尾疑问语气词(只看结尾两个字,避免"吗"出现在别处造成误判)
    tail = t.rstrip("。，、！~～… \t")
    if tail.endswith(("吗", "呢", "吧", "么", "嘛")):
        return True
    return False


def _follows_mine(history, group: str, sender: str, now: float,
                  window: float = 180.0) -> bool:
    """这条是不是"在回应机器人刚说的话"。

    严格的判据(三层都要满足,避免把无关闲聊认成接话):
      1. 机器人在这个群最近 window 秒内发过言
      2. **此人自己上一条消息在机器人那条之前** —— 说明机器人那条是夹在中间的
      3. 本条紧跟在机器人那条之后

    通俗说:我先说话 → 机器人回我 → 我接着说 = 在跟机器人聊。
    """
    if not history:
        return False

    recent = [m for m in history if getattr(m, "group", None) == group]
    if not recent:
        return False

    # 最近一条机器人的发言
    my_idx = -1
    for i in range(len(recent) - 1, -1, -1):
        if getattr(recent[i], "is_self", False):
            my_idx = i
            break
    if my_idx < 0:
        return False

    mine = recent[my_idx]
    my_ts = float(getattr(mine, "timestamp", 0) or 0)
    if not my_ts or (now - my_ts) > window:
        return False

    # ⚠ 判据 3 必须真校验:本条要在机器人那条**之后**。
    #   漏了这一步时,同一次 tick 里 A 的提问让机器人回了话,
    #   紧接着判定 B 的闲聊 —— 机器人的新回复刚好成了"最近一条自发言",
    #   而 B 自己的旧消息在它之前 ⇒ 误判成"在接我的话",
    #   进而绕过主动接话冷却,机器人乱插话(实测多回一条)。
    if now < my_ts:
        return False
    # 历史里最后一条非 self 消息必须就是本条(紧跟在机器人之后)
    tail = [m for m in recent if not getattr(m, "is_self", False)]
    if tail:
        last_other = tail[-1]
        if (getattr(last_other, "sender_id", "")
                or getattr(last_other, "sender", "")) != sender:
            return False

    # 再确认:此人在这条之前的上一条,在机器人那条之前
    who = sender
    for i in range(my_idx - 1, -1, -1):
        m = recent[i]
        if getattr(m, "is_self", False):
            continue
        if (getattr(m, "sender_id", "") or getattr(m, "sender", "")) == who:
            # 找到此人上一条:它在机器人发言之前 => 成立
            return True
        # 遇到别人说的话就先不算"接我的话"
        return False
    # 机器人发言之前找不到此人说过话 —— 也可能是他在回应,宽松放行
    return True


def should_chime_in(msg, history, my_names: List[str],
                    now: Optional[float] = None) -> Tuple[bool, str, bool]:
    """规则级判断:这条没 @ 我的消息要不要主动接。

    返回 (要不要接, 原因, 是不是"在回应我")。
    第三项为真时调用方应绕过"主动接话冷却" —— 别人正接你的话,
    这时候必须回,否则就是装死。
    """
    # 语音消息的 content 是 "[语音]",真正的话在 voice_text —— 两个都要看
    raw = (msg.content or "").strip()
    vt = (getattr(msg, "voice_text", "") or "").strip()
    text = vt if (vt and raw in ("", "[语音]")) else raw
    now = now if now is not None else time.time()

    if getattr(msg, "is_self", False):
        return False, "自己的消息", False
    if _is_noise(text):
        return False, "纯语气/表情", False
    if _at_someone_else(text, my_names):
        return False, "在跟别人说话(@了别人)", False

    # 在回应我刚才说的话 => 最该接的情况
    if _follows_mine(history, msg.group, msg.sender_id or msg.sender, now):
        return True, "在接我刚才的话", True

    # 像在问问题(而且没指定问谁)=> 可以接
    if _looks_like_question(text) and len(text) >= 4:
        return True, "像在问问题", False

    return False, "跟我不相关", False


# ---------- 模型级判断(可选,更准但费 token) ----------

JUDGE_PROMPT = (
    "你是一个微信群里的普通群友。下面给你群里最近的聊天记录。\n"
    "请判断:最新那条消息,你【作为群里的一员】该不该主动开口接一句?\n\n"
    "该接的情况:\n"
    "- 有人在问问题,但没明确问谁(可能是问大家)\n"
    "- 话题正好接着你刚说的话,人家在回应你\n"
    "- 明显是在讨论你、提到你、cue 你\n\n"
    "不该接的情况:\n"
    "- 两个人在互相聊天,跟你无关\n"
    "- 只是在发表情、附和、复读\n"
    "- 在聊你完全不懂也没参与的话题\n"
    "- 已经有人在回答那个问题了\n\n"
    "只回答一个词:接 或者 不接。不要解释。\n\n"
    "聊天记录:\n%s\n\n最新消息是「%s」说的:「%s」\n"
)


def judge_by_model(llm, conversation: str, sender: str, text: str) -> Tuple[bool, str]:
    """让模型判断要不要接。失败时返回不接(宁可少说)。"""
    try:
        raw = llm.simple_complete(
            JUDGE_PROMPT % (conversation[:1500], sender, text[:200]),
            max_tokens=6, temperature=0.0)
        ans = (raw or "").strip()
        if ans.startswith("接"):
            return True, "模型判断该接"
        return False, "模型判断不接"
    except Exception:
        return False, "模型判断失败"
