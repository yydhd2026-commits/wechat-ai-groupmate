# -*- coding: utf-8 -*-
"""群消息模型:与微信实现解耦,便于离线测试。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

# @ 名字后面允许出现的"边界"字符。
# 微信真实 @ 用 U+2005 分隔,必须包含;其余是常见标点/空白。
_AT_SEPS = set(" \t\r\n\u2005\u00a0\u3000"
               ":：,，.。!！?？;；、/\\|()（）[]【】<>《》\"'“”‘’—-_~～+*")

# 消息里 @ 了某个名字(用于判断"是不是在跟别人说话")
import re as _re
_AT_OTHER = _re.compile(r"@(?!\s*$)[^\s@]{1,20}")


@dataclass
class GroupMessage:
    """一条群消息。所有后端(数据库/UIA/测试桩)都归一成这个结构。"""
    group: str                 # 群名
    sender: str                # 发送者显示名
    content: str               # 正文
    timestamp: float = field(default_factory=time.time)
    msg_id: str = ""           # 后端能提供就用,用于去重
    is_self: bool = False      # 是不是机器人自己发的
    mentioned_me: bool = False  # 是否 @ 了机器人
    sender_id: str = ""        # 稳定 id(群昵称可能重复时用)
    # 图片/媒体:本地已解密好的文件路径(后端填充,没有则为空)
    image_path: str = ""
    # 语音:本地已解出的 .silk 路径,以及识别出来的文字
    voice_path: str = ""
    voice_text: str = ""
    # 原始消息类型(库里的 type 字段),用于判断是不是图片/语音
    msg_type: str = ""
    # 消息所在的分片库文件名(跨分片同号时需要,下载媒体用)
    shard: str = ""

    @property
    def has_image(self) -> bool:
        return bool(self.image_path)

    @property
    def is_image_msg(self) -> bool:
        return ("图片" in (self.msg_type or "")) or (self.content or "").strip() == "[图片]"

    @property
    def is_voice_msg(self) -> bool:
        return ("语音" in (self.msg_type or "")) or (self.content or "").strip() == "[语音]"

    @property
    def key(self) -> str:
        """去重键。

        ⚠ 必须带 shard:微信的 local_id 只在分片内唯一,跨分片会重复
        (库源码 db.py 明确写了这点)。只用 群|local_id 会把两条不同的消息
        判成"重复消息",第二条永远不回。
        """
        if self.msg_id:
            return f"{self.group}|{self.shard}|{self.msg_id}"
        return f"{self.group}|{self.sender}|{self.timestamp:.3f}|{self.content[:32]}"


def build_history_text(messages: List[GroupMessage], bot_name: str,
                       limit: int = 12) -> str:
    """把近期消息拼成给模型看的聊天记录。

    limit<=0 表示"不带历史";注意别写成 messages[-0:],那等于整段历史。
    """
    msgs = messages
    if limit and limit > 0:
        msgs = messages[-limit:]
    lines = []
    for m in msgs:
        who = "我" if m.is_self else m.sender
        text = (m.content or "").replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


def is_at_me(text: Optional[str], mention_names: Optional[List[str]]) -> bool:
    """文本里有没有「@机器人名」，且名字后面是**边界**。

    ⚠ 边界为什么重要(从 PersonaMorph 源码学到的教训):
    老实现是 `"@" + name in text` 的纯子串判断,于是
      「@Ngyydhdhd 你好」(别人昵称以机器人名开头) 会被误判成 @ 我,
      白烧一次模型调用,还可能乱插话。
    现在要求名字后面必须是分隔符或结束,并且大小写不敏感。

    另:微信真实 @ 用的分隔符是 U+2005(四分之一空格),必须算进边界表。
    """
    if not text or not mention_names:
        return False
    t = text.casefold()
    for name in mention_names:
        n = str(name or "").strip()
        if not n:
            continue
        low = n.casefold()
        i = 0
        while True:
            i = t.find("@" + low, i)
            if i < 0:
                break
            j = i + 1 + len(low)
            if j >= len(t) or t[j] in _AT_SEPS:
                return True
            i += 1
    return False


def _at_someone_else(text: Optional[str], my_names: List[str]) -> bool:
    """消息里 @ 了别人(不是 @ 我)。"""
    hits = _AT_OTHER.findall(text or "")
    if not hits:
        return False
    mine = {"@" + n for n in (my_names or []) if n}
    for h in hits:
        if h not in mine:
            return True
    return False


def strip_mention(text: Optional[str],
                  mention_names: Optional[List[str]] = None) -> str:
    """剥掉正文开头的 @某人,只留下问题本身。

    微信的 @ 有两种形态:
      1. 纯文本 "@Ngyydhd 你好"
      2. 带不可见占位符 "@\u2005Ngyydhd\u2005你好"
         (真实 @ 会把名字和后面的文字用 U+2005 之类隔开)

    ⚠ 边界坑:有人手打成 "@小助手你好"(名字和正文粘在一起)。
    老实现找不到分隔符就把**整条消息清空** —— 等于问题被丢掉,
    模型只收到"（没写字，只发了内容）",答非所问。
    现在:只有确认剥掉的是配置里的机器人名字时才整体剥,
    否则原样返回。宁可让 @ 留在文本里,也不要把问题吃掉。
    """
    if not text:
        return ""
    s = text.replace("\uFFFC", " ").strip()

    known = [n for n in (mention_names or []) if n]
    # 长名优先,免得短的把长的截断(如 "小助手" vs "小助手bot")
    names = sorted(known, key=len, reverse=True) if known else None

    while s.startswith("@"):
        rest = s[1:]
        # 找第一个真正的空白(U+2005 等也算)
        cut = -1
        for i, ch in enumerate(rest):
            if ch in " \t\u2005\u00a0\u3000":
                cut = i
                break

        if cut > 0:
            s = rest[cut:].strip()          # 正常形态:"@名字 正文"
            continue

        # 没有分隔符:判断是不是"@名字正文"粘在一起
        if names:
            hit = next((n for n in names if rest.startswith(n)), "")
            if hit and len(rest) > len(hit):
                s = rest[len(hit):].strip()  # 剥掉名字,留下正文
                continue
            if hit:
                s = ""                        # 整条就是 @名字
                break
            break                             # @的不是机器人 -> 原样保留
        # 没给名字表可依据:退回老行为
        s = ""
        break

    # 清掉残留的不可见占位符
    for ch in ("\u2005", "\u00a0", "\u3000"):
        s = s.replace(ch, " ")
    return " ".join(s.split())


def is_quiet_hour(hour_start: int, hour_end: int, now: Optional[float] = None) -> bool:
    """深夜静默判定,支持跨零点(如 23 -> 7)。"""
    if hour_start == hour_end:
        return False
    h = time.localtime(now if now is not None else time.time()).tm_hour
    if hour_start < hour_end:
        return hour_start <= h < hour_end
    return h >= hour_start or h < hour_end
