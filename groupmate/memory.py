# -*- coding: utf-8 -*-
"""记忆系统。

分三层:
  1. 短期上下文  —— 每个群最近 N 条消息(内存 + 落盘,重启不丢)
  2. 长期事实    —— 关于群里的人和各种事,定期让模型从聊天里提炼
  3. 关键词索引  —— 被 @ 时按关键词取出相关记忆,拼进 prompt

设计原则:
  * 全部落盘到 JSON,重启后继续用
  * 只增不删(除非超过上限),避免把学会的东西丢掉
  * 提炼用便宜的调用,并且限流,避免烧 token
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


@dataclass
class Fact:
    """一条记忆事实。

    借鉴 PersonaMorph 的时效规则:不同类别的记忆寿命不一样 ——
    时间承诺会过期,生日永不淡出,寒暄一次性作废。
    """
    text: str                  # 事实内容,如"张三喜欢打篮球"
    group: str = ""            # 属于哪个群(空=全局)
    about: str = ""            # 关于谁(昵称/wxid,可空)
    ts: float = field(default_factory=time.time)
    hits: int = 0              # 被引用次数,用于淘汰
    kind: str = "general"      # general / promise / birthday / preference / fact
    expires_at: float = 0.0    # 0 = 永不过期

    def key(self) -> str:
        return (self.group + "|" + self.about + "|" + self.text).lower()

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() > self.expires_at

    def age_text(self) -> str:
        """给模型看的「多久之前」。"""
        d = time.time() - self.ts
        if d < 3600:
            return "刚刚"
        if d < 86400:
            return "%d 小时前" % int(d // 3600)
        if d < 86400 * 30:
            return "%d 天前" % int(d // 86400)
        return "%d 个月前" % int(d // (86400 * 30))


# 各类记忆的寿命(秒)。0 = 永不过期
KIND_TTL = {
    "general": 0,                    # 一般事实:长期
    "fact": 0,
    "preference": 0,                 # 偏好:长期
    "birthday": 0,                   # 生日/纪念日:永不淡出
    "promise": 86400 * 30,           # 时间承诺:30 天过期
    "mood": 86400 * 1,               # 临时情绪:1 天
}


def classify_fact(text: str) -> str:
    """按内容猜事实类别,决定寿命。"""
    t = text or ""
    if any(k in t for k in ("生日", "纪念日", "周年")):
        return "birthday"
    if any(k in t for k in ("答应", "约定", "约好", "明天", "后天", "下周",
                            "要交", "截止", "之前给", "记得给")):
        return "promise"
    if any(k in t for k in ("喜欢", "爱玩", "爱吃", "讨厌", "偏好", "常玩", "本命")):
        return "preference"
    if any(k in t for k in ("生气", "不爽", "郁闷", "难过", "烦")):
        return "mood"
    return "general"


class Memory:
    def __init__(self, path: str, context_limit: int = 40,
                 max_facts: int = 800):
        self.path = path
        self.context_limit = context_limit
        self.max_facts = max_facts
        # group -> [消息 dict]
        self.contexts: Dict[str, List[dict]] = {}
        self.facts: List[Fact] = []
        self.notes: Dict[str, str] = {}       # group -> 群印象摘要
        self._dirty = False
        # 加载失败标记:置位后拒绝 save,避免用空结构覆盖用户的记忆
        self._load_failed = False
        self._load_error = ""
        self.load()

    # ---------------- 落盘 ----------------

    def load(self) -> None:
        """读记忆文件。

        ⚠ 两条铁律(踩过坑):
          1. **逐条解析**:某一条 fact 多了/少了一个字段就整份清空,
             再一 save 就把用户的记忆永久抹掉。坏条目跳过即可。
          2. **解析失败就禁写**:文件读坏了宁可不写,也不能用空结构覆盖它。
        """
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            # 文件本身读不了 -> 禁止覆盖,保留原文件等人工处理
            self._load_failed = True
            self._load_error = str(e)[:120]
            return

        if not isinstance(data, dict):
            self._load_failed = True
            self._load_error = "顶层不是 JSON 对象"
            return

        # contexts:必须是 dict[str, list]
        ctx = data.get("contexts")
        if isinstance(ctx, dict):
            self.contexts = {str(k): v for k, v in ctx.items()
                             if isinstance(v, list)}
        else:
            self.contexts = {}

        # notes:必须是 dict[str, str]
        nt = data.get("notes")
        if isinstance(nt, dict):
            self.notes = {str(k): str(v) for k, v in nt.items()}
        else:
            self.notes = {}

        # facts:逐条解析,坏的跳过(不要因为一条坏数据丢掉整库记忆)
        facts: List[Fact] = []
        raw_facts = data.get("facts")
        bad = 0
        if isinstance(raw_facts, list):
            for item in raw_facts:
                if not isinstance(item, dict) or not item.get("text"):
                    bad += 1
                    continue
                try:
                    known = set(Fact.__dataclass_fields__)
                    facts.append(Fact(**{k: v for k, v in item.items()
                                         if k in known}))
                except Exception:
                    bad += 1
        self.facts = facts
        if bad:
            self._load_error = "跳过 %d 条无法解析的记忆" % bad

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        # 加载失败过 => 拒绝覆盖,免得把坏文件变成空文件(记忆永久丢失)
        if getattr(self, "_load_failed", False):
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            # 写之前先留一份备份,出问题还能手工救回来
            if os.path.exists(self.path):
                try:
                    import shutil
                    shutil.copy2(self.path, self.path + ".bak")
                except Exception:
                    pass
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "contexts": self.contexts,
                    "notes": self.notes,
                    "facts": [asdict(x) for x in self.facts],
                    "saved_at": time.time(),
                }, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception:
            pass

    # ---------------- 短期上下文 ----------------

    def remember(self, group: str, sender: str, sender_id: str, text: str,
                 is_self: bool = False, is_image: bool = False,
                 ts: Optional[float] = None) -> None:
        buf = self.contexts.setdefault(group, [])
        buf.append({
            "s": sender,
            "sid": sender_id,
            "t": (text or "")[:200],
            "me": bool(is_self),
            "img": bool(is_image),
            "ts": ts or time.time(),
        })
        # 每个群只留最近的,避免文件膨胀
        if len(buf) > self.context_limit * 3:
            del buf[: -self.context_limit]
        self._dirty = True

    def context_text(self, group: str, limit: Optional[int] = None) -> str:
        buf = self.contexts.get(group) or []
        limit = limit or self.context_limit
        lines = []
        for m in buf[-limit:]:
            who = "我" if m.get("me") else (m.get("s") or "群友")
            t = (m.get("t") or "").replace("\n", " ").strip()
            if m.get("img") and not t:
                t = "[发了张图片]"
            if not t:
                continue
            lines.append("%s: %s" % (who, t))
        return "\n".join(lines)

    # ---------------- 长期事实 ----------------

    def add_fact(self, text: str, group: str = "", about: str = "",
                 kind: str = "", ttl: float = 0.0) -> bool:
        text = (text or "").strip()
        if not text or len(text) < 4:
            return False
        kind = kind or classify_fact(text)
        if not ttl:
            ttl = KIND_TTL.get(kind, 0)
        f = Fact(
            text=text, group=group, about=about, kind=kind,
            expires_at=(time.time() + ttl) if ttl else 0.0,
        )
        existing = {x.key() for x in self.facts}
        if f.key() in existing:
            return False
        self.facts.append(f)
        # 超上限:优先淘汰「已过期的、没被引用过的、最老的」。
        # ⚠ 排序方向很容易写反:升序后取尾部,等于专挑 expired=True 的留下。
        #   这里明确按"保留价值"排序,取前 max_facts 条。
        if len(self.facts) > self.max_facts:
            self.facts.sort(
                key=lambda x: (not x.expired,      # 没过期的排前面
                               x.hits,             # 被引用多的排前面
                               x.ts),              # 新的排前面
                reverse=True)
            self.facts = self.facts[: self.max_facts]
        self._dirty = True
        return True

    def purge_expired(self) -> int:
        """清掉过期记忆(生日/偏好这类永不过期,不会被动)。"""
        before = len(self.facts)
        self.facts = [f for f in self.facts if not f.expired]
        n = before - len(self.facts)
        if n:
            self._dirty = True
        return n

    def relevant(self, group: str, query: str, about: str = "",
                 limit: int = 8) -> List[Fact]:
        """取出和当前对话最相关的记忆。

        打分:同群 > 全局;提到的人 > 没提到;关键词命中 + 引用次数。
        过期的记忆直接跳过(时间承诺会过期,生日永不淡出)。
        """
        q = (query or "").lower()
        toks = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{2,}", q))
        scored = []
        for f in self.facts:
            if f.expired:
                continue                      # 过期的不再拿出来
            score = 0.0
            # 生日/纪念日加权:这类记忆永远该主动想起来
            if f.kind == "birthday":
                score += 2.0
            if f.group == group:
                score += 3.0
            elif f.group:
                continue                      # 别的群的事不掺和
            if about and f.about and (about in f.about or f.about in about):
                score += 3.0
            if toks:
                ft = f.text.lower()
                hit = sum(1 for t in toks if t in ft)
                score += hit * 1.5
            if f.about and f.about.lower() in q:
                score += 1.0
            score += min(f.hits, 3) * 0.2
            if score > 0:
                scored.append((score, f))
        scored.sort(key=lambda x: (x[0], x[1].ts), reverse=True)
        chosen = [f for _, f in scored[:limit]]
        for f in chosen:
            f.hits += 1
        if chosen:
            self._dirty = True
        return chosen

    def memory_text(self, group: str, query: str = "", about: str = "") -> str:
        """拼成给模型看的『你记得的事』。

        借鉴 PersonaMorph:每条记忆带一行「多久之前」,让模型知道新旧,
        而不是把三周前和今天的事当成同一时间发生。
        """
        parts = []
        note = self.notes.get(group)
        if note:
            parts.append("【你对这个群的印象】\n" + note)
        rel = self.relevant(group, query, about)
        if rel:
            lines = []
            for f in rel:
                who = ("关于%s:" % f.about) if f.about else ""
                age = f.age_text()
                tag = ""
                if f.kind == "promise":
                    tag = "[约定]"
                elif f.kind == "birthday":
                    tag = "[纪念日]"
                lines.append("- %s%s %s（%s）" % (tag, who, f.text, age))
            parts.append("【你记得的事】\n" + "\n".join(lines))
        return "\n\n".join(parts)

    def set_note(self, group: str, note: str) -> None:
        if note and note.strip():
            self.notes[group] = note.strip()[:1000]
            self._dirty = True

    def stats(self) -> dict:
        return {
            "groups": len(self.contexts),
            "facts": len(self.facts),
            "context_msgs": sum(len(v) for v in self.contexts.values()),
        }
