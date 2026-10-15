# -*- coding: utf-8 -*-
"""决策引擎:决定「这条消息要不要回」。

刻意做成纯函数式 + 可注入时钟,因此能完全离线测试——
这是整个机器人里最需要被验证、也最容易出错的部分。
"""
from __future__ import annotations

import os
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

from .config import BotConfig
from .message import GroupMessage, is_at_me, is_quiet_hour
from .proactive import should_chime_in


@dataclass
class Decision:
    """决策结果。reply=False 时 reason 说明为什么不说。"""
    reply: bool
    reason: str
    trigger: str = ""          # mention / keyword / random
    forced: bool = False       # 被 @ 时即使限流也不该静默——但会排队


@dataclass
class _Bucket:
    stamps: Deque[float] = field(default_factory=deque)


class ReplyPolicy:
    """护栏 + 触发规则。所有状态都在内存里,可持久化到磁盘由 Bot 负责。"""

    def __init__(self, cfg: BotConfig, now_fn=None, rand=None):
        self.cfg = cfg
        # 时间基准。
        # ⚠ 这里必须是"单调的消息时间游标",不是裸的 time.time():
        #   冷却/静默全部按消息时间轴比较,而机器人追赶积压时消息时间戳
        #   会落后于真实时钟 —— 两套基准混用会让差值变负,老消息被误判成
        #   "刚回过"而静默丢弃(@ 必回会偶发失效)。
        #   游标 = max(已见消息时间戳, 真实时钟),只增不减,两边都成立。
        #   测试传入 now_fn 时直接用它(保证可注入)。
        self._now_fn = now_fn
        self._seen_wall: dict = {}          # group -> 消息时间戳高水位
        self._now = now_fn or self._clock
        self._rand = rand or random.random
        self._last_user_reply: dict = {}
        self._last_group_reply: dict = {}
        self._last_self_msg: dict = {}
        self._minute: _Bucket = _Bucket()
        self._hour: _Bucket = _Bucket()
        self._day: _Bucket = _Bucket()
        self._consecutive_failures = 0
        self.seen: set = set()
        # 「谁在哪个群刚 @ 过我」——供"图片跟进"判定
        self._last_mention: dict = {}
        self._last_mention_who: dict = {}
        # 主动接话的冷却(每群最后一次主动开口的时间)
        self._last_proactive: dict = {}
        # 每群最近的主动开口时间戳(deque),用于"每小时最多 N 次"的总量闸门
        self._proactive_history: dict = {}
        self._proactive_reason = ""
        self._proactive_why = ""

    # ---------- 主动接话的量控 ----------

    def _proactive_over_hour_quota(self, group: str, now: float) -> bool:
        """这一小时主动开口的次数是不是已经超了。

        为什么冷却还不够:群里热闹时消息一条接一条,每次冷却刚过就又有
        "像在问问题"的消息,结果还是显得话多。加一道总量闸门才真压得住。
        """
        quota = int(getattr(self.cfg.trigger, "proactive_max_per_hour", 0) or 0)
        if quota <= 0:
            return False
        hist = self._proactive_history.get(group)
        if not hist:
            return False
        # 清掉一小时前的
        while hist and now - hist[0] > 3600.0:
            hist.popleft()
        return len(hist) >= quota

    def _note_proactive(self, group: str, now: float) -> None:
        """记一次主动开口(用于每小时配额)。"""
        hist = self._proactive_history.get(group)
        if hist is None:
            hist = deque()
            self._proactive_history[group] = hist
        hist.append(now)
        while hist and now - hist[0] > 3600.0:
            hist.popleft()

    # ---------- 护栏 ----------

    def export_state(self) -> dict:
        """导出护栏状态,供重启后恢复。

        ⚠ 为什么必须持久化:限流桶、连续失败计数、各种冷却全在内存里,
        一重启就归零。如果机器人因为故障反复重启,每次都重新计时 ——
        "每分钟 12 条"这类承诺就形同虚设,连续失败熔断也会被重置,
        结果可能是重启后立刻又猛发一轮。
        """
        import time as _t
        now = _t.time()
        return {
            "ts": now,
            # 只留还在窗口内的计数,避免把过期数据也搬过去
            "minute": [s for s in self._minute.stamps if now - s <= 60.0],
            "hour": [s for s in self._hour.stamps if now - s <= 3600.0],
            "day": [s for s in self._day.stamps if now - s <= 86400.0],
            "failures": self._consecutive_failures,
        }

    def import_state(self, data: dict) -> None:
        """恢复护栏状态。时间太久远的计数会被丢弃(自然过期)。"""
        if not isinstance(data, dict):
            return
        import time as _t
        now = _t.time()

        def _take(key, window):
            out = []
            for s in (data.get(key) or []):
                try:
                    s = float(s)
                except (TypeError, ValueError):
                    continue
                if 0 < now - s <= window:
                    out.append(s)
            return out

        self._minute.stamps = deque(sorted(_take("minute", 60.0)))
        self._hour.stamps = deque(sorted(_take("hour", 3600.0)))
        self._day.stamps = deque(sorted(_take("day", 86400.0)))

        try:
            self._consecutive_failures = max(0, int(data.get("failures") or 0))
        except (TypeError, ValueError):
            self._consecutive_failures = 0

    def kill_switch_active(self) -> bool:
        p = self.cfg.guard.kill_switch_file
        return bool(p) and os.path.exists(p)

    def _prune(self, now: float) -> None:
        g = self.cfg.guard
        for bucket, window in ((self._minute, 60.0), (self._hour, 3600.0),
                               (self._day, 86400.0)):
            while bucket.stamps and now - bucket.stamps[0] > window:
                bucket.stamps.popleft()

    def guard_check(self, now: float) -> Tuple[bool, str]:
        g = self.cfg.guard
        self._prune(now)
        if self.kill_switch_active():
            return False, "紧急停止开关已触发(%s 存在)" % g.kill_switch_file
        if len(self._minute.stamps) >= g.max_per_minute:
            return False, "触发每分钟上限(%d)" % g.max_per_minute
        if len(self._hour.stamps) >= g.max_per_hour:
            return False, "触发每小时上限(%d)" % g.max_per_hour
        if len(self._day.stamps) >= g.max_per_day:
            return False, "触发每日上限(%d)" % g.max_per_day
        if self._consecutive_failures >= g.max_consecutive_failures:
            return False, "连续失败 %d 次,已自动暂停" % self._consecutive_failures
        return True, ""

    # ---------- 时间基准 ----------

    def _clock(self) -> float:
        """单调时间游标:全局"已见时间戳"的高水位。

        只增不减,保证任何"现在 − 上次回复"的差值都不为负 ——
        这是"@ 必回"不被误杀的关键(机器人追赶积压时,消息时间戳会落后于
        真实时钟,若拿 time.time() 当基准,差值成负数会被判成"刚回过")。

        ⚠ 没有任何记录时退回真实时钟:否则游标会从 0 开始,
        第一次冷却比较算出天文数字,判断完全失真。
        """
        if not self._seen_wall:
            import time as _t
            return _t.time()
        return max(self._seen_wall.values())

    def note_wall(self, group: str, ts: float) -> float:
        """记录该群的时间戳高水位,返回**更新后的全局游标**。

        ⚠ 两处容易写错的细节:
          1. 用 >= 而不是 >:时间戳正好等于当前高水位时也要认,
             否则"0 秒"这种合法时间戳会被整个丢掉(实测踩到,
             导致冷却判定彻底失效)。
          2. 返回全局值:_now() 取所有群的最大值,调用方要接着用它当基准,
             不能只看本群那一格。
        """
        try:
            ts = float(ts or 0)
        except (TypeError, ValueError):
            return self._clock()
        if ts >= self._seen_wall.get(group, float("-inf")):
            self._seen_wall[group] = ts
        return self._clock()

    # ---------- 触发判定 ----------

    def _detect_trigger(self, msg: GroupMessage) -> Tuple[bool, str]:
        t = self.cfg.trigger
        # ⚠ 语音消息的 content 是 "[语音]" 占位符,真正的话在 voice_text。
        #   以前只读 content,导致"用语音 @ 我"完全判不出来 ——
        #   整套语音识别 + 补试队列白做,只能靠 voice_follow 兜。
        text = msg.content or ""
        vt = getattr(msg, "voice_text", "") or ""
        probe = text if text.strip() not in ("", "[语音]") else (vt or text)

        if msg.mentioned_me:
            self._mark_mention(msg)
            return True, "mention"
        # ⚠ 用带边界的匹配,不能用 "@名字" in text 的纯子串:
        #   否则 "@小助手bot"(别人昵称以机器人名开头) 会被误唤醒。
        if is_at_me(probe, t.mention_names):
            self._mark_mention(msg)
            return True, "mention"
        for kw in t.keywords:
            if kw and kw in probe:
                return True, "keyword"

        # 图片跟进:有人刚 @ 过我,紧接着发的图片也算触发
        # (很多人习惯先 @ 一下,再单独把图发出来)
        if msg.is_image_msg and not msg.is_self:
            last = self._last_mention.get(msg.group)
            if last is not None and (msg.timestamp - last) <= t.image_follow_seconds:
                who = self._last_mention_who.get(msg.group)
                if not who or who == (msg.sender_id or msg.sender):
                    return True, "image_follow"

        # 语音跟进:同上 —— 先 @ 一下、再发语音条,也是在跟我说话。
        # 语音识别出来的文字如果本身带 @,上面那条 mention 分支已经拦到了。
        if msg.is_voice_msg and not msg.is_self:
            last = self._last_mention.get(msg.group)
            if last is not None and (msg.timestamp - last) <= t.image_follow_seconds:
                who = self._last_mention_who.get(msg.group)
                if not who or who == (msg.sender_id or msg.sender):
                    return True, "voice_follow"

        # 主动插话:先看冷却,再掷骰子(避免随机概率被冷却"吃掉"日志)
        if t.random_reply_probability > 0 and self._rand() < t.random_reply_probability:
            return True, "random"
        return False, ""

    def _mark_mention(self, msg: GroupMessage) -> None:
        """记住「谁在哪个群刚 @ 过我」,供图片跟进判定使用。"""
        self._last_mention[msg.group] = msg.timestamp
        self._last_mention_who[msg.group] = msg.sender_id or msg.sender

    def decide(self, msg: GroupMessage, history: Optional[List[GroupMessage]] = None
               ) -> Decision:
        t = self.cfg.trigger
        # ⚠ 时间基准(踩过坑):
        #   · 深夜静默要用**消息挂钟小时** -> wall
        #   · 冷却/静默/限流要用**单调的消息时间游标** -> now
        #   为什么不能直接用 msg.timestamp:机器人追赶积压时,老消息的
        #   时间戳在"上次回复时间"之前,差值成负数 -> 被误判成"刚回过"而
        #   静默丢消息(@ 必回会偶发失效)。
        #   为什么不能直接用 time.time():真实时钟与消息时间轴是两套基准,
        #   混用会让冷却窗口失去意义。
        #   游标取"已见消息时间戳的高水位",单调不回退,两边都满足。
        wall = msg.timestamp or 0.0
        # 喂高水位(单调,不回退),然后取统一的时间基准。
        # ⚠ now 必须包含本条消息的时间戳,否则"这条消息比上次回复晚多少"
        #   会被算成 0(游标还停在上一条),防抖/冷却会把该回的消息误杀。
        cursor = self.note_wall(msg.group, wall)
        now = max(cursor, wall, self._now())

        # 1. 去重
        if msg.key in self.seen:
            return Decision(False, "重复消息")
        self.seen.add(msg.key)
        if len(self.seen) > 5000:
            self.seen = set(list(self.seen)[-2000:])

        # 2. 自己发的消息绝不回应(防自我对话),并刷新自我静默计时
        if msg.is_self:
            self._last_self_msg[msg.group] = now
            return Decision(False, "机器人自己发的消息")

        # 3. 群白名单
        if not t.groups:
            return Decision(False, "未配置任何群(trigger.groups 为空)")
        if msg.group not in t.groups:
            return Decision(False, "群不在白名单: %s" % msg.group)

        # 4. 空消息(图片/语音消息没有正文,不能当空消息丢掉)
        if not (msg.content or "").strip() and not (
                msg.is_image_msg or msg.is_voice_msg):
            return Decision(False, "空消息")

        # 5+6. 深夜静默 与 触发判定
        #
        # ⚠ 顺序很关键:必须先判定"是不是 @ 我",再决定要不要静默。
        #   老实现先查静默,于是**凌晨被 @ 也直接拒绝** —— 和 README/
        #   配置注释承诺的"静默时段 @ 仍然会回"完全相反。
        #   规则(代码 / 文档 / 测试三者一致):
        #     · @ 我、以及它的图片/语音跟进 -> **穿透静默**,必回
        #     · 关键词、主动接话 -> 静默时段不开口
        triggered, trigger = self._detect_trigger(msg)
        force_triggers = ("mention", "image_follow", "voice_follow")
        if trigger not in force_triggers:
            if len(t.quiet_hours) == 2 and is_quiet_hour(
                    t.quiet_hours[0], t.quiet_hours[1], wall):
                return Decision(False, "深夜静默时段")

        # 6b. 没被 @ 时:看要不要"主动接一句"。
        #     很多人不 @ 也会问问题、或者接着机器人的话往下说,
        #     这种该接的要接上,但必须有频率上限,不能变话痨。
        #     ⚠ 三道闸门一起管,只靠"冷却时间"是不够的 ——
        #       群里聊得越热闹,消息到达越密,光冷却也拦不住它一直插嘴:
        #         ① 冷却:两次主动接话之间至少隔 proactive_cooldown_seconds
        #         ② 总量:每群每小时最多 proactive_max_per_hour 次
        #         ③ 门槛:只接"明确像在问问题"的(见 proactive_strict_question)
        #     两条豁免:
        #       · @ 永远不受这些限制(被点名必须答)
        #       · 「在接我刚才说的话」不受冷却限制 —— 人家正跟你聊,装死最像机器人
        if not triggered and t.proactive_enabled:
            last_p = self._last_proactive.get(msg.group)
            in_cool = (last_p is not None
                       and now - last_p < t.proactive_cooldown_seconds)
            # 总量闸门:冷却之外再看"这一小时已经主动说过几次"
            over_quota = self._proactive_over_hour_quota(msg.group, now)
            want, why, replying_to_me = should_chime_in(
                msg, history or [], t.mention_names, now,
                strict_question=bool(getattr(
                    t, "proactive_strict_question", True)),
                min_chars=int(getattr(t, "proactive_min_chars", 6) or 6))
            if want and (replying_to_me or (not in_cool and not over_quota)):
                triggered, trigger = True, "proactive"
                self._proactive_reason = why
                self._proactive_why = why

        if not triggered:
            return Decision(False, "没被@，也判断为不用接")

        # 7. 冷却(@ 和「图片跟进」可以突破冷却,否则会显得"叫不动")。
        #    图片跟进是同一个交互的延续:先 @ 再发图,不该因为冷却被丢掉。
        #    注意顺序:冷却必须在「自我静默」之前判定,否则一次"主动插话"
        #    之后会把后续的冷却/回复全部误判成"刚发过言"。
        if trigger not in ("mention", "image_follow", "voice_follow"):
            lu = self._last_user_reply.get(msg.sender_id or msg.sender)
            if lu is not None and now - lu < t.per_user_cooldown_seconds:
                return Decision(False, "对 %s 的冷却中" % msg.sender)
            lg = self._last_group_reply.get(msg.group)
            if lg is not None and now - lg < t.per_group_cooldown_seconds:
                return Decision(False, "本群冷却中")

        # 8. 刚发过言就先安静一会儿(防自问自答刷屏)。
        #    @ 和图片跟进【完全绕过】这个静默 —— 要求是「连续两个 @ 都要回答,不能遗漏」,
        #    所以被点名时绝不因为"我刚说过话"而装死。
        #    只保留一个很短的防抖(mention_debounce_seconds,默认 0=不防抖),
        #    用来挡住"同一条消息被重复投递"这种真正重复的情况。
        if trigger not in ("mention", "image_follow", "voice_follow"):
            last_self = self._last_self_msg.get(msg.group)
            if last_self is not None and now - last_self < t.self_silence_seconds:
                return Decision(False, "刚发过言,静默中")
        elif t.mention_debounce_seconds > 0:
            last_self = self._last_self_msg.get(msg.group)
            if last_self is not None and now - last_self < t.mention_debounce_seconds:
                return Decision(False, "刚回过,防抖中(%.1fs)" % t.mention_debounce_seconds)

        # 9. 硬护栏
        ok, why = self.guard_check(now)
        if not ok:
            return Decision(False, why, trigger=trigger, forced=(trigger == "mention"))

        return Decision(True, "ok", trigger=trigger)

    # ---------- 记账 ----------

    def record_reply(self, msg: GroupMessage, now: Optional[float] = None) -> None:
        """记账:记下"刚刚回了一条",供冷却/静默/限流使用。

        ⚠ 记账的时间必须是**冷却判定时用的那个时间**(也就是单调游标),
        不能听调用方随手传的值。踩过的坑:
          · 传真实时钟 time.time() -> 与消息时间轴差几万秒,冷却全部失真;
          · 传一个比游标还早的 msg.timestamp -> 冷却窗口被算成天文数字,
            "刚回过就别再回"直接失效(实测:连续两条 @ 都回了)。
        所以这里的规则是:以游标为准;调用方传的值只有在**更新**时才采纳。
        """
        cur = self.note_wall(msg.group, msg.timestamp or 0.0)
        if now is not None and now > cur:
            cur = self.note_wall(msg.group, now)
        now = cur
        self._last_user_reply[msg.sender_id or msg.sender] = now
        self._last_group_reply[msg.group] = now
        self._last_self_msg[msg.group] = now
        # 主动接话单独计时(不走 _last_group_reply,否则 @ 之后的冷却会互相干扰)
        if self._proactive_reason:
            self._last_proactive[msg.group] = now
            self._note_proactive(msg.group, now)   # 计入"每小时最多几次"
            self._proactive_reason = ""      # 用完即清,免得下次误记
        for b in (self._minute, self._hour, self._day):
            b.stamps.append(now)
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def resume(self) -> None:
        """人工恢复:清零连续失败计数。"""
        self._consecutive_failures = 0
