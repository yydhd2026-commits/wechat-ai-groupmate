# -*- coding: utf-8 -*-
"""大模型客户端:OpenAI 兼容接口,默认指向 DeepSeek。

只依赖标准库 urllib,少一个装不上的依赖。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import List, Optional

from .config import LLMConfig
from .message import GroupMessage, build_history_text, strip_mention


class LLMError(RuntimeError):
    pass


MAX_IMAGE_SIDE = 1600          # 图片最长边超过这个就压缩,控制请求体积
MAX_IMAGE_BYTES = 3 * 1024 * 1024   # 压缩后仍超过 3MB 就跳过

# 微信表情代码用的方括号词。只认这些,别把 [1] [答案] 这种误伤。
_WX_EMOJI = {
    "微笑", "撇嘴", "色", "发呆", "得意", "流泪", "害羞", "闭嘴", "睡", "大哭",
    "尴尬", "发怒", "调皮", "呲牙", "惊讶", "难过", "酷", "冷汗", "抓狂", "吐",
    "偷笑", "可爱", "白眼", "傲慢", "饥饿", "困", "惊恐", "流汗", "憨笑", "大兵",
    "奋斗", "咒骂", "疑问", "嘘", "晕", "折磨", "衰", "骷髅", "敲打", "再见",
    "擦汗", "抠鼻", "鼓掌", "糗大了", "坏笑", "左哼哼", "右哼哼", "哈欠", "鄙视",
    "委屈", "快哭了", "阴险", "亲亲", "吓", "可怜", "菜刀", "西瓜", "啤酒",
    "篮球", "乒乓", "咖啡", "饭", "猪头", "玫瑰", "凋谢", "示爱", "爱心", "心碎",
    "蛋糕", "闪电", "炸弹", "刀", "足球", "瓢虫", "便便", "月亮", "太阳", "礼物",
    "拥抱", "强", "弱", "握手", "胜利", "抱拳", "勾引", "拳头", "差劲", "爱你",
    "NO", "OK", "爱情", "飞吻", "跳跳", "发抖", "怄火", "转圈", "磕头", "回头",
    "跳绳", "挥手", "激动", "街舞", "献吻", "左太极", "右太极", "笑脸", "生病",
    "破涕为笑", "捂脸", "皱眉", "耶", "吃瓜", "加油", "汗", "天啊", "Emm",
    "社会社会", "旺柴", "好的", "打脸", "哇", "翻白眼", "666", "让我看看",
    "叹气", "苦涩", "裂开", "淡定", "绝望", "emmm", "黑人问号", "感动",
    "发呆", "机智", "耶", "吃瓜", "加油", "打脸", "我酸了", "太难了", "无语",
}

_EMOJI_RE = re.compile(r"\[([^\[\]\n]{1,8})\]")


def limit_emoji(text: str, max_count: int = 1) -> str:
    """一条消息里最多留 max_count 个微信表情代码。

    模型经常"顺手多带几个",人设里写了也未必遵守,所以程序兜一刀。
    只处理方括号里确实是微信表情词的那些,[1][答案] 这类不受影响。
    """
    if not text or max_count < 0:
        return text
    seen = [0]

    def _sub(m):
        word = m.group(1)
        if word not in _WX_EMOJI:
            return m.group(0)          # 不是表情代码,原样保留
        seen[0] += 1
        return m.group(0) if seen[0] <= max_count else ""

    out = _EMOJI_RE.sub(_sub, text)
    # 删掉表情后可能留下多余空格
    out = re.sub(r"[ \u3000]{2,}", " ", out).strip()
    return out


def _to_data_url(path: str) -> Optional[str]:
    """把本地图片转成 data URL。会按需压缩,避免请求过大。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as _f:
            raw = _f.read()
        ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
        if ext == "jpg":
            ext = "jpeg"
        # 小图直接用
        if len(raw) <= MAX_IMAGE_BYTES:
            return "data:image/%s;base64,%s" % (ext, base64.b64encode(raw).decode())
        # 大图用 PIL 压缩
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(raw))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            w, h = im.size
            scale = min(1.0, MAX_IMAGE_SIDE / max(w, h))
            if scale < 1.0:
                im = im.resize((int(w * scale), int(h * scale)))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82)
            data = buf.getvalue()
            if len(data) > MAX_IMAGE_BYTES:
                return None
            return "data:image/jpeg;base64," + base64.b64encode(data).decode()
        except Exception:
            return None
    except Exception:
        return None


class ChatLLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._api_key = cfg.resolve_api_key()

    @property
    def ready(self) -> bool:
        return bool(self._api_key)

    def reply(self, msg: GroupMessage, history: List[GroupMessage],
              system_prompt: str, images: Optional[List[str]] = None,
              mention_names: Optional[List[str]] = None) -> str:
        """生成回复。images 是本地图片路径列表(有图时走视觉模型)。"""
        if not self._api_key:
            raise LLMError(
                "没有 API Key。请设置环境变量 %s,或写进 config.json 的 llm.api_key"
                % self.cfg.api_key_env)

        history_text = build_history_text(history, bot_name="", limit=self.cfg.history_limit)

        question = strip_mention(msg.content, mention_names)
        if not question:
            question = "（没写字，只发了内容）"

        if images:
            usable = []
            for p in images[:4]:      # 最多 4 张,避免请求过大
                data_url = _to_data_url(p)
                if data_url:
                    usable.append(data_url)

            if usable:
                # 按**实际附上的张数**描述,否则"说发了 3 张、实际 0 张"会让模型困惑
                user_prompt = (
                    f"群里最近的聊天记录（供参考语境）：\n{history_text}\n\n"
                    f"现在「{msg.sender}」在群里点名问你，并发了 {len(usable)} 张图片。\n"
                    f"Ta 说的是：「{question}」\n\n"
                    f"请仔细看图片内容（可能是作业题、截图、照片等），"
                    f"直接回答 Ta 的问题，用群里聊天的口吻，简洁自然。"
                    f"如果是题目就把过程和答案写清楚，但别太啰嗦。"
                )
                content = [{"type": "text", "text": user_prompt}]
                for u in usable:
                    content.append({"type": "image_url", "image_url": {"url": u}})
                user_content = content
            else:
                # 图取不到/太大 —— 如实告诉模型,别让它以为自己看到了图
                user_content = (
                    f"群里最近的聊天记录（供参考语境）：\n{history_text}\n\n"
                    f"现在「{msg.sender}」在群里点名问你。"
                    f"Ta 本来发了图片，但这边没能取到图片内容。\n"
                    f"Ta 说的是：「{question}」\n\n"
                    f"请照常回答 Ta 的问题；如果必须要看图才能答，"
                    f"就用群聊口吻说一句图没加载出来、让 Ta 重发。"
                )
        else:
            user_prompt = (
                f"群里最近的聊天记录（供参考语境）：\n{history_text}\n\n"
                f"现在「{msg.sender}」在群里点名问你：\n"
                f"「{question}」\n\n"
                f"请直接回答这个具体问题，用群里聊天的口吻，简短自然。"
            )
            user_content = user_prompt

        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        raw = self._post("/chat/completions", payload)
        try:
            text = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("返回结构异常: %s" % json.dumps(raw, ensure_ascii=False)[:300]) from e
        return (text or "").strip()

    # ---------------- 轻量调用(用于判断类任务) ----------------

    def simple_complete(self, prompt: str, max_tokens: int = 8,
                        temperature: float = 0.0) -> str:
        """单轮短问答,用于"要不要接话"这类判断。失败抛异常由调用方兜。"""
        if not self._api_key:
            raise LLMError("没有 API Key")
        raw = self._post("/chat/completions", {
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        })
        try:
            return (raw["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("返回结构异常") from e

    # ---------------- 记忆提炼 ----------------

    def extract_facts(self, conversation: str, group: str) -> dict:
        """从一段聊天记录里提炼值得记住的事实。

        返回 {"facts": ["..."], "note": "群印象摘要"}
        提炼失败不抛异常,返回空结果 —— 记忆失败不该影响回复。
        """
        if not self._api_key or not (conversation or "").strip():
            return {"facts": [], "note": ""}

        prompt = (
            "下面是一个微信群最近的聊天记录。请从中提炼【值得长期记住】的信息，"
            "用于以后聊天时参考。\n\n"
            "只记这类内容：\n"
            "- 群里的人是谁、叫什么、有什么明显特点或爱好\n"
            "- 他们正在做的事、在聊的话题、计划、约定\n"
            "- 明确的偏好、习惯、身份信息（比如「小明在准备考研」）\n"
            "- 群里常用的梗、黑话、称呼\n\n"
            "不要记：一次性的闲聊、无意义的语气词、脏话、临时情绪。\n\n"
            "输出严格用这个 JSON 格式（不要加代码块标记）：\n"
            '{"facts": [{"text": "事实内容", "kind": "类别"}], '
            '"note": "对这个群的一句话印象"}\n\n'
            "kind 只能取这几个值之一：\n"
            "- promise    约定/承诺/截止时间（会过期）\n"
            "- birthday   生日/纪念日（永不过期）\n"
            "- preference 偏好/爱好（长期）\n"
            "- fact       其他长期事实\n\n"
            "facts 最多 6 条，每条 text 不超过 40 字，用第三人称陈述。"
            "如果这段记录里没有值得记的，facts 就返回 []。\n\n"
            "聊天记录：\n" + conversation[:3000]
        )
        try:
            raw = self._post("/chat/completions", {
                "model": self.cfg.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 600,
                "stream": False,
            })
            text = raw["choices"][0]["message"]["content"].strip()
            # 去掉可能的 ```json 包裹
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            data = json.loads(text)

            items = data.get("facts") or []
            facts = []
            for it in items[:6]:
                if isinstance(it, dict):
                    t = str(it.get("text") or "").strip()
                    k = str(it.get("kind") or "").strip()
                else:
                    t, k = str(it).strip(), ""
                if t:
                    facts.append((t, k))
            note = str(data.get("note") or "").strip()
            return {"facts": facts, "note": note}
        except Exception:
            return {"facts": [], "note": ""}

    def _post(self, path: str, payload: dict) -> dict:
        url = self.cfg.base_url.rstrip("/") + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self._api_key)

        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                # 4xx 里只有 429 值得重试
                if e.code in (429, 500, 502, 503, 504):
                    last_err = LLMError("HTTP %s: %s" % (e.code, detail))
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError("HTTP %s: %s" % (e.code, detail)) from e
            except Exception as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise LLMError("请求失败: %r" % (last_err,))


def sanitize_reply(text: str, max_chars: int) -> str:
    """把模型输出收拾成能直接发进群的样子。"""
    if not text:
        return ""
    t = text.strip()
    # 去掉整段包裹的引号。注意中文引号是成对的不同字符(“/”),不能
    # 用 startswith(q) and endswith(q) 判同字符。
    pairs = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
             ("「", "」"), ("『", "』"), ("《", "》")]
    for left, right in pairs:
        if len(t) > 1 and t.startswith(left) and t.endswith(right):
            t = t[1:-1].strip()
            break
    # 去掉常见自言自语前缀
    for pref in ("回复：", "回复:", "我说：", "我说:", "答：", "答:"):
        if t.startswith(pref):
            t = t[len(pref):].strip()
            break
    # 折叠多余空行(微信里连发多条空行很怪)
    t = "\n".join(line.strip() for line in t.splitlines() if line.strip())

    # 短回复不许分段:模型偶尔把一句话拆成两段、甚至自我否定重说一遍。
    # 只要整体不长、又不是"列步骤"的样子,就压成一行(更像随口说的话)。
    if "\n" in t and len(t) <= 90:
        lines = t.split("\n")
        looks_like_list = any(
            line[:1].isdigit() or line[:1] in "①②③④⑤⑥⑦⑧⑨-•*"
            for line in lines[1:])
        if not looks_like_list:
            t = "".join(lines)

    if len(t) > max_chars:
        t = t[:max_chars].rstrip()

    # 表情代码兜一刀:人设里写了"最多一个",模型未必听
    t = limit_emoji(t, max_count=1)
    return t
