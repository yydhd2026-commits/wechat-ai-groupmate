# -*- coding: utf-8 -*-
"""主循环:监听 → 决策 → 生成 → 发送。"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, List, Optional

from .backend import Backend
from .config import BotConfig
from .llm import ChatLLM, LLMError, sanitize_reply
from .memory import Memory
from .message import GroupMessage, strip_mention
from .policy import ReplyPolicy

Logger = Callable[[str], None]


def _default_logger(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


class GroupMateBot:
    def __init__(self, cfg: BotConfig, backend: Backend, llm: Optional[ChatLLM] = None,
                 logger: Optional[Logger] = None,
                 data_dir: Optional[str] = None):
        """data_dir:记忆/状态文件的落盘目录。

        默认放在项目根(config.json 旁边)。**测试和演示应该显式指定一个
        临时目录**,否则会在仓库里留下 memory.json / bot_state.json ——
        既是垃圾,也可能把真实的聊天记忆混进要提交的代码里。
        """
        self.cfg = cfg
        self.backend = backend
        self.llm = llm or ChatLLM(cfg.llm)
        self.log = logger or _default_logger
        self.policy = ReplyPolicy(cfg)
        self.history: List[GroupMessage] = []
        # 每群的完整上下文(读所有消息,供被 @ 时参考)
        self.context: dict = {}
        self._context_limit = 40
        # 落盘目录:没指定就用项目根
        self._data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..")
        # 长期记忆:落盘,重启不丢
        self.memory = Memory(
            os.path.join(self._data_dir, "memory.json"),
            context_limit=self._context_limit)
        self.stats = {"seen": 0, "replied": 0, "skipped": 0, "failed": 0}
        # 已在处理中消费掉的消息(避免 tick 里重复处理补发的内容)
        self._handled: set = set()
        # 刚发出去的内容:群 -> [(文本, 时间)]。用于拦截"自己的消息被读回来"
        # 微信会把机器人自己发的消息再读出来(还可能有多份),
        # 不拦就会自己触发自己,出现连发好几条。
        self._recent_sent: dict = {}
        self._sent_window = 180.0        # 3 分钟内算"刚发的"
        # 发送间隔冷却用的"消息时间轴"(决策层用 msg.timestamp,与真实时钟分开)
        self._recent_sent_ts: dict = {}
        # 待转写的语音:msg_id -> [消息, 首次尝试时间, 已试次数]
        # 微信下载语音要 30~60 秒,一次拿不到不能丢,后台持续重试。
        self._pending_voices: dict = {}
        self._voice_retry_window = 180.0   # 最多等 3 分钟
        self._voice_max_tries = 12
        # 等待补发窗口里"顺手"取回来的消息。它们已被后端缓冲取走,
        # 必须留到下一轮处理,否则就是静默丢消息。
        self._deferred: List[GroupMessage] = []
        # 被限流挡住的 @ —— 排队等配额恢复后补答。
        # "被点名必答"是硬承诺,但限流是暂时的,所以是**延后**不是丢弃。
        # 每项:[key, msg, 入队时间, 已试次数]
        self._pending_mentions: list = []
        self._mention_retry_window = 300.0   # 最多等 5 分钟
        self._mention_retry_max = 10
        # 记忆提炼节流:每个群累计多少条新消息后提炼一次
        self._learn_every = int(getattr(cfg, "learn_every_messages", 25) or 25)
        self._since_learn: dict = {}
        # 记录「实际发出/打算发出」的内容,便于演示与审计
        self.outbox: List[tuple] = []
        self._stop = False
        self._state_file = os.path.join(self._data_dir, "bot_state.json")

    # ---------- 生命周期 ----------

    def stop(self) -> None:
        self._stop = True

    def run(self, poll_interval: float = 1.0, max_loops: Optional[int] = None) -> dict:
        self.log("启动。群白名单=%s 干跑=%s 模型=%s"
                 % (self.cfg.trigger.groups or "(空,不会回应任何群)",
                    self.cfg.dry_run, self.cfg.llm.model))
        ms = self.memory.stats()
        self.log("记忆已加载: %d 个群 / %d 条事实 / %d 条上下文"
                 % (ms["groups"], ms["facts"], ms["context_msgs"]))
        # 恢复护栏状态(限流计数 / 连续失败)—— 不恢复的话,
        # 机器人反复重启会每次都重新计时,"每分钟上限"就形同虚设。
        try:
            self._load_state()
        except Exception as e:
            self.log("恢复护栏状态失败(不影响运行): %r" % (e,))
        if not self.llm.ready:
            self.log("警告: 没有检测到 API Key(环境变量 %s),回复会失败。"
                     % self.cfg.llm.api_key_env)
        self.backend.start()
        loops = 0
        try:
            while not self._stop:
                self.tick()
                loops += 1
                if max_loops is not None and loops >= max_loops:
                    break
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            self.log("收到 Ctrl+C,停止。")
        finally:
            try:
                self.backend.stop()
            except Exception as e:
                self.log("停止后端时出错: %r" % (e,))
            self.memory.save(force=True)
            self.log("记忆已保存")
        self.log("结束。统计: %s" % self.stats)
        return self.stats

    # ---------- 单轮 ----------

    def _remember(self, msg: GroupMessage) -> None:
        """把消息记进全局历史、该群上下文缓冲、以及长期记忆。"""
        self.history.append(msg)
        if len(self.history) > 500:
            self.history = self.history[-200:]

        buf = self.context.setdefault(msg.group, [])
        buf.append(msg)
        if len(buf) > self._context_limit * 2:
            del buf[: -self._context_limit]

        # 长期记忆(落盘)
        self.memory.remember(
            group=msg.group,
            sender="我" if msg.is_self else msg.sender,
            sender_id=msg.sender_id,
            text=msg.content if not msg.is_image_msg else "",
            is_self=msg.is_self,
            is_image=msg.is_image_msg,
            ts=msg.timestamp,
        )

        # 累计够了就让模型提炼一次记忆
        n = self._since_learn.get(msg.group, 0) + 1
        self._since_learn[msg.group] = n
        if self._learn_every > 0 and n >= self._learn_every:
            self._since_learn[msg.group] = 0
            self._learn(msg.group)

    def _learn(self, group: str) -> None:
        """从该群最近的聊天里提炼长期记忆(失败不影响回复)。"""
        try:
            convo = self.memory.context_text(group, limit=self._context_limit)
            if len(convo) < 30:
                return
            result = self.llm.extract_facts(convo, group)
            added = 0
            for item in (result.get("facts") or []):
                if isinstance(item, (list, tuple)):
                    text = item[0] if item else ""
                    kind = item[1] if len(item) > 1 else ""
                else:
                    text, kind = str(item), ""
                if self.memory.add_fact(text, group=group, kind=kind):
                    added += 1
            note = result.get("note") or ""
            if note:
                self.memory.set_note(group, note)
            # 顺手清掉过期记忆
            purged = self.memory.purge_expired()
            if added or note or purged:
                self.memory.save()
                self.log("记忆更新:「%s」+%d 条(共 %d 条)%s"
                         % (group, added, len(self.memory.facts),
                            (",清理过期 %d 条" % purged) if purged else ""))
        except Exception as e:
            self.log("记忆提炼失败(不影响回复): %r" % (e,))

    def _group_context(self, group: str) -> List[GroupMessage]:
        """该群的最近上下文(包含所有消息,不只被触发的)。"""
        return self.context.get(group, [])[-self._context_limit:]

    def _is_own_echo(self, msg: GroupMessage) -> bool:
        """判断这条是不是"机器人自己刚发出去、又被读回来"的消息。

        微信数据库里自己的消息会带 wxid 前缀被当成新消息读出,
        有时一份内容还能出现好几条 —— 不拦就会自己触发自己,连发多条。
        """
        if msg.is_self:
            return True
        text = (msg.content or "").strip()
        if not text:
            return False
        now = time.time()
        for prev, ts in self._recent_sent.get(msg.group, []):
            if now - ts > self._sent_window:
                continue
            # 完全相同 -> 一定是自己的回声
            if text == prev:
                return True
            # ⚠ 前缀判定必须加双向长度限制。老实现只限了本条长度:
            #   机器人上一条回的是"好的",180 秒内有人发"好的 那我明天再问你"
            #   就会被当成自己的回声 —— 而且回声过滤发生在 _remember 之前,
            #   这条真人消息既不回复、也不进上下文/记忆,彻底消失。
            #   规则:短消息只认完全相等;只有"发出去的长消息被微信截断"才算回声。
            if len(prev) >= 8 and prev.startswith(text):
                return True
        return False

    def _self_id(self) -> str:
        """机器人自己的 wxid(拿不到就空串)。"""
        for attr in ("_self_wxid", "self_wxid", "account"):
            v = getattr(self.backend, attr, "")
            if isinstance(v, str) and v:
                return v
        return ""

    def _note_sent(self, group: str, text: str, ts: Optional[float] = None) -> None:
        """记下刚发出的内容,供 _is_own_echo / 发送冷却使用。

        ⚠ 时间基准必须用**真实时钟**:回声判断(_is_own_echo)拿 time.time() 去比,
        如果这里存消息时间戳,机器人追赶积压消息时(消息时间戳落后几分钟)
        `now - ts` 会变成负数,窗口判断直接失效 —— 踩过这个坑。

        冷却需要的"消息时间轴"由 _recent_sent_ts 单独维护,两者不混用。
        """
        now = time.time()
        lst = self._recent_sent.setdefault(group, [])
        lst.append((text.strip(), now))
        # 只留窗口内的(以最新一条为基准),避免无限增长
        latest = max(s for _t, s in lst)
        self._recent_sent[group] = [(t, s) for t, s in lst
                                    if latest - s <= self._sent_window][-20:]
        # 供"发送间隔冷却"用的消息时间轴(决策层用的是 msg.timestamp)
        if ts is not None:
            self._recent_sent_ts[group] = ts

    def tick(self) -> None:
        try:
            messages = self.backend.poll()
        except Exception as e:
            self.log("取消息失败: %r" % (e,))
            return

        # 先把图片下载好再交给决策层 —— 这样 _collect_images 拿到的是现成文件。
        # 注意必须把 messages 传进去:poll() 已清空内部缓冲,不传就找不到待下载的图。
        if any(getattr(m, "is_image_msg", False) for m in messages):
            fn = getattr(self.backend, "ensure_images", None)
            if callable(fn):
                try:
                    n = fn(messages)
                    if n:
                        self.log("已下载 %d 张图片" % n)
                except Exception as e:
                    self.log("图片下载出错(不影响文字回复): %r" % (e,))

        # 语音:先把 SILK 音频取下来,再把能识别的转成文字
        if getattr(self.cfg.voice, "enabled", False) and \
                any(getattr(m, "is_voice_msg", False) for m in messages):
            fn = getattr(self.backend, "ensure_voices", None)
            if callable(fn):
                try:
                    n = fn(messages)
                    if n:
                        self.log("已提取 %d 段语音" % n)
                except Exception as e:
                    self.log("语音提取出错(不影响文字回复): %r" % (e,))
            for m in messages:
                if getattr(m, "is_voice_msg", False) and m.voice_path \
                        and not m.voice_text:
                    self._transcribe(m)

        # 后台重试之前没拿到音频的语音。
        # 微信下载语音要 30~60 秒 —— 一次拿不到就丢会变成"有时行有时不行",
        # 所以放进队列持续重试,拿到了就补一句(只补一次,不刷屏)。
        try:
            self._retry_pending_voices()
        except Exception as e:
            self.log("语音补试出错(不影响别的): %r" % (e,))

        # 后台补答之前被限流挡住的 @ —— "被点名必答"不能因为暂时超限就丢
        try:
            self._retry_pending_mentions()
        except Exception as e:
            self.log("待回复补答出错(不影响别的): %r" % (e,))

        # 定期清理媒体缓存目录(每来一条语音都会落盘 .silk + .wav,
        # 长期跑下去会把磁盘吃满)。每 200 轮清一次就够。
        self._ticks_done = getattr(self, "_ticks_done", 0) + 1
        if self._ticks_done % 200 == 0:
            try:
                self._prune_media()
            except Exception as e:
                self.log("清理缓存失败(不影响运行): %r" % (e,))

        # 先处理上一轮等待窗口里"顺手取回来"的消息 —— 它们已经从后端缓冲
        # 里取走了,不接住就永久丢失(踩过这个大坑:@ 必回变成静默丢失)。
        pending = self._deferred
        self._deferred = []
        if pending:
            self.log("接上上一轮遗留的 %d 条消息" % len(pending))
        messages = list(pending) + list(messages)

        # 无论回不回复,所有消息都进上下文(但要先滤掉自己的回声)
        fresh = []
        for msg in messages:
            if self._is_own_echo(msg):
                self.stats["skipped"] += 1
                continue
            self.stats["seen"] += 1
            self._remember(msg)
            fresh.append(msg)

        # 只对"该回"的消息走决策/生成流程
        for msg in fresh:
            # 补发的内容已经作为"后续消息"被合并处理过,不再单独处理
            if msg.key in self._handled:
                self._handled.discard(msg.key)
                continue
            self.handle(msg, siblings=fresh)

        # _handled 用 key 而不是 id():对象释放后 id 会被复用,可能误判;
        # 而且集合会无限增长,这里加个上限裁剪。
        if len(self._handled) > 500:
            self._handled = set(list(self._handled)[-200:])

    def handle(self, msg: GroupMessage, siblings: Optional[List[GroupMessage]] = None) -> Optional[str]:
        """处理单条消息,返回实际发出的文本(None 表示没发)。

        siblings: 本轮 tick 一起取回来的其它消息。很多人习惯先 @ 一下、
                  再把问题或图片发出来 —— 那些补发的内容就在里面,
                  必须合并理解,不能让它变成"只看到一个 @"。
        """
        decision = self.policy.decide(msg, self.history)

        if not decision.reply:
            self.stats["skipped"] += 1
            # 只记一行,避免刷屏;被限流挡住的 @ 单独提醒
            brief = (msg.content or "").replace("\n", " ")[:20]
            if decision.forced:
                # ⚠ @ 被限流挡住时**不能就这么丢了** ——
                #   "被点名必答"是硬承诺,限流只是暂时的。
                #   放进待回复队列,等配额恢复后补答(仍有次数/时限上限,
                #   绝不会因为"必回"就突破安全护栏)。
                self.log("⚠ @我 但被限流挡住,已排队等待配额恢复: %s"
                         % decision.reason)
                self._queue_mention(msg)
            else:
                self.log("读取 [%s] %s" % (msg.group, brief))
            return None

        # 刚在这个群回复过 -> 短时间内不再接话。
        # 群里经常在机器人回复后紧接着又 @ 一次,连着答会很吵;
        # 而且自己的消息被读回来时也更容易触发连锁回复。
        # 群里的连续回复不要太多 —— 但这个冷却【绝不挡 @】:
        # 被点名的优先级最高,连续两个 @ 必须都答,不能因为"我刚说过话"就装死。
        # 只对非 @ 的触发(关键词/主动接话)生效。
        # ⚠ 时间基准统一用「消息时间戳」,不要用 time.time() ——
        #   两者混用会导致冷却判断错乱(踩过这个坑)。
        if decision.trigger not in ("mention", "image_follow", "voice_follow"):
            cooldown = float(getattr(self.cfg.trigger,
                                     "post_send_cooldown_seconds", 12.0))
            last_ts = self._recent_sent_ts.get(msg.group)
            if cooldown > 0 and last_ts is not None:
                since = msg.timestamp - last_ts
                if since < cooldown:
                    self.stats["skipped"] += 1
                    self.log("读取 [%s] %s（刚回复过 %.0fs,先不接话）"
                             % (msg.group,
                                (msg.content or "").replace("\n", " ")[:20],
                                since))
                    return None

        # 等待并收集"紧接着补发"的内容(文字 + 图片)
        extra_text, images, consumed = self._gather_followups(msg, siblings)
        for m in consumed:
            self._handled.add(m.key)

        # 组装要问模型的问题:本条正文 + 补发的文字 + 语音识别结果
        question_parts = []
        base = strip_mention(msg.content, self.cfg.trigger.mention_names)
        if base:
            question_parts.append(base)
        # 语音:本条是语音就用识别出的文字;补发的语音也算
        if msg.is_voice_msg and msg.voice_text:
            vt = strip_mention(msg.voice_text, self.cfg.trigger.mention_names)
            if vt and vt not in question_parts:
                question_parts.append(vt)
        for t in extra_text:
            if t and t not in question_parts:
                question_parts.append(t)
        # 补发消息里的语音
        for m in consumed:
            if getattr(m, "is_voice_msg", False) and m.voice_text:
                vt = strip_mention(m.voice_text, self.cfg.trigger.mention_names)
                if vt and vt not in question_parts:
                    question_parts.append(vt)
        combined = "\n".join(question_parts).strip()
        if combined:
            msg.content = combined
        if msg.is_voice_msg and not msg.voice_text:
            # 音频还没下载好/识别失败 —— 不装死,也不马上说"听不了",
            # 先放进补试队列;等真拿到文字了再回,免得答非所问。
            self._queue_voice(msg)
            self.log("这段语音暂时没拿到文字,已加入补试队列")
            self.stats["skipped"] += 1
            return None
        if combined and (extra_text or images or msg.is_voice_msg):
            self.log("合并后的问题:「%s」| 图片 %d 张"
                     % (combined.replace("\n", " ")[:50], len(images)))

        # 用"该群的完整上下文"(不只触发消息)喂给模型
        ctx = self._group_context(msg.group)

        # 拼人设 + 记忆
        sys_prompt = self.cfg.persona.build_system_prompt()
        mem = self.memory.memory_text(msg.group, query=combined,
                                      about=msg.sender)
        if mem:
            sys_prompt += "\n\n" + mem

        # 生成回复(走统一的方法:护栏检查与记账只写一次)
        text = self._generate_reply(msg, trigger=decision.trigger,
                                    images=images, extra_context=ctx)
        if text is None:
            return None
        # ⚠ 这里传 msg.timestamp(消息时间轴),不要传 time.time():
        #   发送冷却(_recent_sent_ts)是按消息时间戳比较的,混入真实时钟
        #   会让差值变成负数,冷却整个失效(实测踩到)。
        if self._send_and_record(msg, text, decision.trigger,
                                 msg.timestamp or time.time()):
            return text
        return None


    # ---------- 统一的发送出口 ----------

    def _send_and_record(self, msg: GroupMessage, text: str, trigger: str,
                         now: Optional[float] = None) -> bool:
        """**所有**对外发送都必须走这里。

        为什么要收口:以前有 4 条各自发送的路径(普通回复 / 干跑 /
        语音补答 / 语音听不了提示),每条的护栏检查和记账都不一样 ——
        结果语音提示那条既没过限流、也没计入配额(实测查出来的 bug)。
        统一到一个出口后,"发之前查护栏、发之后记账"只写一次,不会再漏。

        返回 True 表示确实发出去了(干跑视为已发)。
        """
        if not text:
            return False
        now = now if now is not None else time.time()

        # ---- 干跑:只打印,不发送,但仍记账(便于观察配额消耗)----
        if self.cfg.dry_run:
            self.log("[干跑] 本想发到「%s」: %s" % (msg.group, text))
            self.outbox.append((msg.group, text, "dry_run"))
            self._note_sent(msg.group, text, msg.timestamp)
            self.policy.record_reply(msg)
            self.stats["replied"] += 1
            return True

        # ---- 发送前统一过护栏:限流 / 熔断 / 紧急停止 ----
        ok_guard, why = self.policy.guard_check(now)
        if not ok_guard:
            self.log("发送被护栏挡住(稍后再试): %s" % why)
            return False

        try:
            sent = self.backend.send(msg.group, text)
        except Exception as e:
            sent = False
            self.log("发送异常: %r" % (e,))

        if not sent:
            self.policy.record_failure()
            self.stats["failed"] += 1
            self.log("发送失败(第 %d 次连续失败)"
                     % self.policy.consecutive_failures)
            return False

        # ---- 发送成功:统一记账 ----
        # ⚠ _note_sent 用真实时钟(供回声拦截),这里不能改;
        #   但**记进上下文的对象必须用消息时间轴** —— 否则自己的发言会带着
        #   真实时钟进 history,和别人的消息时间戳相差极大,
        #   proactive._follows_mine 会算出"我上一句是一百多天前说的",
        #   "对方接着我的话往下说"这个判断就永远不成立(实测踩到)。
        self._note_sent(msg.group, text, msg.timestamp)
        self.policy.record_reply(msg, now)
        self.stats["replied"] += 1
        self.outbox.append((msg.group, text, trigger))
        try:
            self._remember(GroupMessage(
                group=msg.group, sender="我", content=text,
                timestamp=msg.timestamp or now, is_self=True,
                sender_id=self._self_id(),
                msg_id="self_%s" % (msg.msg_id or int(now * 1000))))
        except Exception as e:
            self.log("记录自己的发言失败(不影响发送): %r" % (e,))
        extra = ""
        if trigger == "proactive":
            why2 = getattr(self.policy, "_proactive_why", "") or "判断该接"
            extra = "（主动接:%s）" % why2
            self.policy._proactive_why = ""
        self.log("[已发送] 「%s」(%s触发)%s: %s"
                 % (msg.group, trigger, extra, text))
        self._save_state()
        return True

    # ---------- 待回复队列(被限流挡住的 @)----------

    def _queue_mention(self, msg: GroupMessage) -> None:
        """把被限流挡住的 @ 排队,等配额恢复后补答。

        注意:只是**延后**,不是**绕过**。补答时仍要过 guard_check,
        所以次数/时限上限一样管着它,绝不会因为"必回"就突破安全护栏。
        """
        key = msg.key
        if any(k == key for k, _m, _t, _n in self._pending_mentions):
            return
        if len(self._pending_mentions) >= 20:      # 硬上限,防止堆积
            self.log("待回复队列已满(20),丢弃最旧的一条")
            self._pending_mentions.pop(0)
        self._pending_mentions.append([key, msg, time.time(), 0])
        self.log("待回复队列 +1(当前 %d 条)" % len(self._pending_mentions))

    def _retry_pending_mentions(self) -> None:
        """每轮 tick 试一次:配额恢复了就把欠下的 @ 补上。"""
        if not self._pending_mentions:
            return
        now = time.time()
        done = []
        for item in list(self._pending_mentions):
            key, msg, first_try, tries = item
            if now - first_try > self._mention_retry_window \
                    or tries >= self._mention_retry_max:
                self.log("待回复放弃(超时/试满): %s"
                         % (msg.content or "")[:24])
                done.append(item)
                continue
            ok_guard, _why = self.policy.guard_check(now)
            if not ok_guard:
                item[3] = tries + 1
                continue                          # 配额还没恢复,下轮再试

            item[3] = tries + 1
            text = self._generate_reply(msg, trigger="mention")
            if text is None:
                continue                          # 生成失败,留给下轮
            if self._send_and_record(msg, text, "mention", now):
                self.log("欠下的 @ 已补答: %s" % text[:40])
                done.append(item)

        for item in done:
            try:
                self._pending_mentions.remove(item)
            except ValueError:
                pass

    # ---------- 生成回复(供各条路径复用)----------

    def _generate_reply(self, msg: GroupMessage, trigger: str = "",
                        images: Optional[List[str]] = None,
                        extra_context: str = "") -> Optional[str]:
        """问模型要一段回复文本。失败返回 None(由调用方决定是否重试)。"""
        ctx = extra_context or self._group_context(msg.group)
        sys_prompt = self.cfg.persona.build_system_prompt()
        try:
            mem = self.memory.memory_text(msg.group, query=msg.content or "",
                                          about=msg.sender)
            if mem:
                sys_prompt += "\n\n" + mem
        except Exception:
            pass
        try:
            raw = self.llm.reply(msg, ctx, sys_prompt,
                                 images=images or None,
                                 mention_names=self.cfg.trigger.mention_names)
        except LLMError as e:
            self.policy.record_failure()
            self.stats["failed"] += 1
            self.log("生成失败(第 %d 次连续失败): %s"
                     % (self.policy.consecutive_failures, e))
            if self.policy.consecutive_failures >= self.cfg.guard.max_consecutive_failures:
                self.log(">>> 连续失败达到上限,已自动暂停发送。修好后删掉状态或重启。")
            return None
        except Exception as e:
            self.policy.record_failure()
            self.stats["failed"] += 1
            self.log("生成异常: %r" % (e,))
            return None

        text = sanitize_reply(raw, self.cfg.trigger.max_reply_chars)
        if not text:
            self.policy.record_failure()
            self.stats["failed"] += 1
            self.log("模型返回空内容,跳过。")
            return None
        if self.cfg.trigger.reply_prefix:
            text = self.cfg.trigger.reply_prefix + text
        return text

    # ---------- 图片收集 ----------

    def _transcriber(self):
        """惰性创建语音识别器(只建一次)。"""
        if getattr(self, "_asr", None) is None:
            from .voice import VoiceTranscriber
            v = self.cfg.voice
            self._asr = VoiceTranscriber(
                base_url=v.base_url, api_key=v.api_key,
                model=v.model, api_key_env=v.api_key_env,
                timeout=v.timeout_seconds)
        return self._asr

    def _transcribe(self, msg: GroupMessage) -> Optional[str]:
        """把一段语音转成文字,写回 msg.voice_text。失败返回 None。

        识别失败绝不影响文字回复 —— 只是这段语音当没听见。
        """
        if msg.voice_text:
            return msg.voice_text
        if not msg.voice_path:
            return None
        try:
            from .voice import silk_to_wav, wav_duration
            v = self.cfg.voice
            if not self._transcriber().ready:
                self.log("语音识别未配置(缺环境变量 %s),这段语音听不了"
                         % v.api_key_env)
                return None

            wav_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "_voices")
            wav = os.path.join(
                wav_dir,
                os.path.splitext(os.path.basename(msg.voice_path))[0] + ".wav")
            os.makedirs(wav_dir, exist_ok=True)
            silk_to_wav(msg.voice_path, wav)

            dur = wav_duration(wav)
            if v.max_seconds and dur > v.max_seconds:
                self.log("语音太长(%.0f 秒 > %.0f 秒上限),跳过"
                         % (dur, v.max_seconds))
                return None

            t0 = time.time()
            text = self._transcriber().transcribe(wav)
            if text:
                msg.voice_text = text
                self.log("语音转文字(%.1f 秒音频, %.1fs 用时): 「%s」"
                         % (dur, time.time() - t0, text[:60]))
                return text
            self.log("语音识别结果为空(%.1f 秒音频)" % dur)
            return None
        except Exception as e:
            self.log("语音识别失败(%s): %s" % (type(e).__name__, str(e)[:120]))
            return None

    # ---------- 磁盘清理 ----------

    def _prune_media(self) -> None:
        """清理图片/语音缓存目录,只留最近的,防止磁盘被吃满。"""
        from .voice import prune_cache, prune_dir
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        n = 0
        # 语音目录:音频后缀
        n += prune_dir(os.path.join(base, "_voices"), keep=300,
                       suffix=(".wav", ".silk", ".pcm"))
        # ⚠ 图片目录必须显式给图片后缀:prune_dir 默认只认音频后缀,
        #   以前这里没传,导致 .jpg 一个都删不掉(磁盘照样被吃满,
        #   日志还照样报"已清理 N 个")。
        n += prune_dir(os.path.join(base, "_images"), keep=300,
                       suffix=(".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
                               ".dat"))
        n += prune_cache(os.path.join(base, "_asr_cache"), keep=2000)
        # 让内存里的 ASR 缓存和磁盘保持一致(否则下次保存又写回来)
        try:
            if getattr(self, "_asr", None) is not None:
                self._asr.trim_cache(2000)
        except Exception:
            pass
        if n:
            self.log("已清理 %d 个过期的媒体缓存文件" % n)

    # ---------- 语音补试队列 ----------

    def _queue_voice(self, msg: GroupMessage) -> None:
        """把没拿到文字的语音放进补试队列(按 msg_id 去重)。"""
        key = "%s|%s" % (msg.group, msg.msg_id or id(msg))
        if key in self._pending_voices:
            return
        self._pending_voices[key] = [msg, time.time(), 0]
        self.log("语音补试队列 +1(当前 %d 条)" % len(self._pending_voices))

    def _retry_pending_voices(self) -> None:
        """每轮 tick 试一次:音频下载好了就转写并补一句回复。

        为什么需要它:微信下载语音要 30~60 秒。一次拿不到就放弃,
        表现就是"有时能识别有时不能"。这里放后台慢慢试,拿到就回。
        """
        if not self._pending_voices:
            return
        now = time.time()
        done: List[str] = []
        for key, item in list(self._pending_voices.items()):
            msg, first_try, tries = item
            # 超时或试太多次就放弃(别无限占资源)
            if now - first_try > self._voice_retry_window \
                    or tries >= self._voice_max_tries:
                self.log("语音补试放弃(%s,试了 %d 次): %s"
                         % (msg.group, tries, (msg.content or "")[:20]))
                # 配置承诺过:实在听不了就如实说一声,别装死
                self._tell_voice_unavailable(msg, tries)
                done.append(key)
                continue

            item[2] = tries + 1
            # 先重新取音频(可能现在下载好了)
            if not msg.voice_path:
                fn = getattr(self.backend, "fetch_voice", None)
                if callable(fn):
                    try:
                        msg.voice_path = fn(msg.group, msg.msg_id) or ""
                    except Exception:
                        msg.voice_path = ""
            if not msg.voice_path:
                continue                      # 还没好,下轮再试

            text = self._transcribe(msg)
            if not text:
                continue                      # 识别没成功,下轮再试

            # 拿到了!补一句
            self.log("语音补试成功,补一句回复: 「%s」" % text[:50])
            try:
                sent = self._reply_to_voice(msg, text)
            except Exception as e:
                sent = False
                self.log("语音补答失败: %r" % (e,))
            # ⚠ 只有真的发出去了才出队。以前无条件 pop,于是
            #   "被护栏挡住 / 发送失败"这类**临时**失败会把队列项删掉;
            #   转写好的文本明明已在 msg.voice_text 和 ASR 缓存里,
            #   却永远不会再被使用 —— 表现就是"那条语音它再也没回"。
            if sent:
                done.append(key)

        for k in done:
            self._pending_voices.pop(k, None)

    def _reply_to_voice(self, msg: GroupMessage, text: str) -> bool:
        """语音补试成功后,基于识别出的文字生成并发送回复。

        返回 True 表示**确实发出去了**(调用方据此决定是否出队)。
        被护栏挡住 / 生成失败 / 发送失败都返回 False —— 这些都是
        临时状态,应该留给补试队列继续尝试。
        """
        if self.cfg.dry_run:
            self.log("[干跑] 本想补答:「%s」" % text[:40])
            return True                      # 干跑视为已完成,不占队列
        if not text.strip():
            return True                      # 没内容可答,终态

        # 注意:护栏检查与记账都在 _send_and_record 里,这里不再重复做 ——
        # 重复检查会让配额被扣两次,行为也对不上。
        ctx = self._group_context(msg.group)
        sys_prompt = self.cfg.persona.build_system_prompt()
        mem = self.memory.memory_text(msg.group, query=text, about=msg.sender)
        if mem:
            sys_prompt += "\n\n" + mem
        # 用一条"虚拟消息"喂给模型,内容就是识别出的文字
        virtual = GroupMessage(
            group=msg.group, sender=msg.sender, content=text,
            timestamp=time.time(), msg_id="voice_%s" % msg.msg_id,
            sender_id=msg.sender_id, mentioned_me=True)
        try:
            raw = self.llm.reply(virtual, ctx, sys_prompt,
                                 mention_names=self.cfg.trigger.mention_names)
        except Exception as e:
            self.policy.record_failure()
            self.log("语音补答生成失败(稍后再试): %r" % (e,))
            return False

        out = sanitize_reply(raw, self.cfg.trigger.max_reply_chars)
        if not out:
            return True                      # 模型没给出内容 = 终态,别死循环
        # ⚠ 走统一出口(内部会过护栏 + 记账 + 记上下文)
        sent = self._send_and_record(msg, out, "voice_follow")
        if sent:
            self.log("[已发送] 「%s」(语音补答): %s" % (msg.group, out))
            return True
        self.log("语音补答发送失败(稍后再试)")
        return False

    def _tell_voice_unavailable(self, msg: GroupMessage, tries: int) -> None:
        """实在听不了这段语音时,按配置决定要不要在群里说一声。

        配置项 voice.tell_when_unavailable 以前全项目没人读 ——
        注释承诺"没有 Key 时在群里如实说",实际只写本地日志,群里永远静默。
        这里补上:每个群最多说一次,免得刷屏。
        """
        try:
            v = self.cfg.voice
            if not getattr(v, "enabled", False):
                return
            if not getattr(v, "tell_when_unavailable", True):
                return
            if self.cfg.dry_run:
                return
            key = "%s|voice_told" % msg.group
            if key in self._handled:
                return
            self._handled.add(key)
            why = ("没配语音识别 Key" if not self._transcriber().ready
                   else "这段语音的音频没能取到")
            note = "语音没听清(%s),打字说呗[捂脸]" % why
            # ⚠ 必须走统一出口:以前这里直接调 backend.send,
            #   既没过限流,也没计入配额 —— 绕过了护栏(实测查出来的)。
            if self._send_and_record(msg, note, "voice_notice"):
                self.log("[已发送] 语音听不了,告知群友: %s" % note)
        except Exception as e:
            self.log("语音提示发送失败(不影响别的): %r" % (e,))

    # ---------- 补发内容收集 ----------

    def followup_wait_seconds(self) -> float:
        return float(getattr(self.cfg.trigger, "image_wait_seconds", 8.0))

    def _gather_followups(self, msg, siblings: Optional[List[GroupMessage]] = None):
        """等待并收集提问者紧接着补发的内容。

        要解决的真实习惯:很多人 @ 一下、然后才把问题或图片发出来。
          @小助手            <- 触发
          这道题怎么做          <- 补发的文字(之前被漏掉)
          [图片]              <- 补发的图片(之前经常下不到)

        两个来源都要看:
          1. siblings —— 本轮 tick 一起取回来的消息(最常见)
          2. 等待期间新到的消息(用户打字慢时)
        图片立刻下载,不留到下一轮(那时它已不在缓冲里,这是"有时识别不了"的根因)。

        返回 (extra_texts, image_paths, consumed_messages)
        """
        group = msg.group
        sender = msg.sender_id or msg.sender
        trigger_is_image = bool(getattr(msg, "is_image_msg", False))

        extra_texts: List[str] = []
        consumed: List[GroupMessage] = []
        candidates: List[GroupMessage] = []

        # 带 @ 的消息必须各自独立触发,绝不能被当成"补发内容"合并吸收。
        # 否则连续两个 @ 会被并成一条:"@我 问题1" + "@我 问题2" 只回一次。
        def _is_own_trigger(m) -> bool:
            if getattr(m, "mentioned_me", False):
                return True
            text = m.content or ""
            for name in getattr(self.cfg.trigger, "mention_names", []) or []:
                if name and ("@" + name) in text:
                    return True
            return False

        # ---------- 1. 本轮同批消息(最可靠) ----------
        for m in (siblings or []):
            if m is msg:
                continue
            if m.group != group:
                continue
            if (m.sender_id or m.sender) != sender:
                continue
            if m.timestamp < msg.timestamp - 30:      # 太旧的不要
                continue
            if _is_own_trigger(m):                    # 它自己会被单独回答
                continue
            candidates.append(m)

        # ---------- 2. 等待期间新到的 ----------
        wait = self.followup_wait_seconds()
        if wait > 0:
            self.log("等待 %.0f 秒,看有没有补发内容…" % wait)
            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(0.5)
                try:
                    extra = self.backend.poll()
                except Exception:
                    extra = []
                for m in extra:
                    self.stats["seen"] += 1
                    self._remember(m)
                    if _is_own_trigger(m):
                        # ⚠ 不能 continue 丢掉!poll() 已经把它从后端缓冲取走了,
                        #   本轮 fresh 又是进 handle 之前就建好的 —— 丢了就再也
                        #   回不来(表现就是"@ 了却没回")。放进下一轮的待处理队列。
                        self._deferred.append(m)
                        continue
                    if m.group == group and (m.sender_id or m.sender) == sender:
                        if m not in candidates:
                            candidates.append(m)
                        if m.is_image_msg:
                            self.log("收到补发的图片(id=%s)" % (m.msg_id or "?"))
                        else:
                            t = strip_mention(m.content, self.cfg.trigger.mention_names)
                            if t:
                                self.log("收到补发的文字:「%s」" % t[:30])
                    else:
                        # 别人的消息同样不能丢,交给下一轮正常判定
                        self._deferred.append(m)
                if any(getattr(m, "is_image_msg", False) for m in extra):
                    fn = getattr(self.backend, "ensure_images", None)
                    if callable(fn):
                        try:
                            n = fn(extra)
                            if n:
                                self.log("已下载 %d 张图片" % n)
                        except Exception as e:
                            self.log("图片下载出错: %r" % (e,))
                # ⚠ 等待窗口收到的语音也要走识别流程。
                #   主循环有 ensure_voices + _transcribe,这条分支以前只处理了图片,
                #   于是"先 @ 再发语音条"时语音虽然有对象、voice_path/voice_text 却是空的,
                #   机器人等于没听懂。这里补上,两条路径行为一致。
                if any(getattr(m, "is_voice_msg", False) for m in extra):
                    fn = getattr(self.backend, "ensure_voices", None)
                    if callable(fn):
                        try:
                            n = fn(extra)
                            if n:
                                self.log("已提取 %d 段语音" % n)
                        except Exception as e:
                            self.log("语音提取出错: %r" % (e,))
                    for m in extra:
                        if getattr(m, "is_voice_msg", False) and m.voice_path \
                                and not m.voice_text:
                            try:
                                self._transcribe(m)
                            except Exception as e:
                                self.log("语音识别出错: %r" % (e,))
        consumed.extend(candidates)

        # ---------- 3. 组装 ----------
        images: List[str] = []
        seen_paths = set()

        if msg.image_path and os.path.exists(msg.image_path):
            images.append(msg.image_path)
            seen_paths.add(msg.image_path)

        # 候选消息里的图片 + 上下文里的图片
        pool = list(candidates) + list(self.context.get(group, [])) + list(self.history)
        # 时间窗放宽到 10 分钟:很多人先发图、过一会儿才 @ 或者补文字,
        # 窗口太窄就会漏掉"刚刚发的图"(踩过这个坑)。
        cutoff = msg.timestamp - float(
            getattr(self.cfg.trigger, "image_lookback_seconds", 600.0))
        for m in pool:
            if m.group != group or m.timestamp < cutoff or not m.is_image_msg:
                continue
            if not trigger_is_image and (m.sender_id or m.sender) != sender:
                continue
            if m.image_path and os.path.exists(m.image_path):
                if m.image_path not in seen_paths:
                    images.append(m.image_path)
                    seen_paths.add(m.image_path)
            elif not m.image_path:
                fn = getattr(self.backend, "ensure_images", None)
                if callable(fn):
                    try:
                        fn([m])
                    except Exception:
                        pass
                if m.image_path and os.path.exists(m.image_path):
                    if m.image_path not in seen_paths:
                        images.append(m.image_path)
                        seen_paths.add(m.image_path)
                else:
                    self.log("图片消息仍未取到文件(id=%s)" % (m.msg_id or "?"))

        # 兜底:上下文里一张图都没有(典型:机器人刚重启,水位跳过了之前的图),
        # 就直接去数据库捞"提问者最近发过的图"。否则会出现
        # "你明明发了图,它却说图是空的"。
        if not images:
            fn = getattr(self.backend, "recent_image_paths", None)
            if callable(fn):
                try:
                    found = fn(group, sender, lookback_seconds=600.0,
                               limit=4)
                    for p in (found or []):
                        if p and p not in seen_paths:
                            images.append(p)
                            seen_paths.add(p)
                    if images:
                        self.log("从数据库补捞到 %d 张早前发的图" % len(images))
                except Exception as e:
                    self.log("补捞图片失败: %r" % (e,))

        # 同一人补发的文字
        for m in candidates:
            if m.is_self or m.is_image_msg:
                continue
            t = strip_mention(m.content, self.cfg.trigger.mention_names)
            if t and t not in extra_texts:
                extra_texts.append(t)

        if images:
            self.log("收集到 %d 张图片,交给视觉模型" % len(images))

        # 等待结束时再检查一次:如果手里还是缩略图,试试升级成清晰版。
        # 很多人 @ 得很快,那会儿微信还没把图下载完,只能拿到缩略图(看不清)。
        # 多等这 8 秒往往就够了。
        upgrader = getattr(self.backend, "upgrade_image", None)
        if callable(upgrader):
            for m in pool:
                if not getattr(m, "is_image_msg", False) or m.is_self:
                    continue
                if m.image_path and "_thumb" in m.image_path:
                    try:
                        if upgrader(m):
                            self.log("图片已升级为清晰版")
                    except Exception as e:
                        self.log("图片升级失败: %r" % (e))
            # 重建 images 列表(可能被升级替换了路径)
            images = []
            seen_paths = set()
            if msg.image_path and os.path.exists(msg.image_path):
                images.append(msg.image_path)
                seen_paths.add(msg.image_path)
            for m in pool:
                if (m.group == group and getattr(m, "is_image_msg", False)
                        and m.image_path and os.path.exists(m.image_path)
                        and m.image_path not in seen_paths):
                    if trigger_is_image or (m.sender_id or m.sender) == sender:
                        images.append(m.image_path)
                        seen_paths.add(m.image_path)

        return extra_texts, images, consumed

    # ---------- 状态 ----------

    def _save_state(self) -> None:
        """把统计 + **护栏状态**一起落盘。

        ⚠ 护栏状态必须存:限流桶、连续失败计数只在内存里,
        重启就归零 —— 故障反复重启时"每分钟上限"会失效,熔断也被重置。
        """
        try:
            payload = {"stats": self.stats, "ts": time.time()}
            try:
                payload["guard"] = self.policy.export_state()
            except Exception:
                pass
            tmp = self._state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_file)
        except Exception:
            pass

    def _load_state(self) -> None:
        """恢复上次落盘的护栏状态。"""
        if not os.path.exists(self._state_file):
            return
        with open(self._state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        guard = (data or {}).get("guard")
        if isinstance(guard, dict):
            self.policy.import_state(guard)
            fd = now = None
            try:
                n = len(self.policy._minute.stamps)
                self.log("护栏状态已恢复: 近1分钟已发 %d 次, 连续失败 %d 次"
                         % (n, self.policy.consecutive_failures))
            except Exception:
                pass
