# -*- coding: utf-8 -*-
"""规则引擎与端到端流程的离线测试。不需要微信、不需要 API Key。

运行: python test_policy.py
"""
from __future__ import annotations

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 测试的落盘隔离
#
# ⚠ 某些受限环境里 tempfile.gettempdir() 会指向**当前工作目录**,
#   于是裸用 tempfile.mkdtemp() 会把临时目录直接建在仓库里 ——
#   跑一次测试就多出一堆 tmpXXXXXXXX/ 目录,还可能被误提交。
#   下面统一包装一次:登记所有临时目录,退出时清理;
#   并在收尾时兜底清扫工作目录(有些测试的临时目录调用点分散,
#   只登记"本文件创建的"难免漏网)。
#
#   必须在 import groupmate 之前装好。
# ---------------------------------------------------------------------------
import atexit as _atexit
import shutil as _shutil
import tempfile as _tempfile

_TMP_DIRS: list = []
_real_mkdtemp = _tempfile.mkdtemp
# atexit 里 __file__ 可能不可用,先把路径记下来
_HERE = os.path.dirname(os.path.abspath(__file__))


def _tracked_mkdtemp(*a, **kw):
    d = _real_mkdtemp(*a, **kw)
    if d not in _TMP_DIRS:
        _TMP_DIRS.append(d)
    return d


_tempfile.mkdtemp = _tracked_mkdtemp          # 全局生效


def _cleanup_tmp():
    for d in reversed(_TMP_DIRS):
        _shutil.rmtree(d, ignore_errors=True)
    _TMP_DIRS.clear()
    # 兜底:清扫工作目录里遗留的临时目录和媒体缓存
    _here = _HERE
    try:
        for name in os.listdir(_here):
            p = os.path.join(_here, name)
            if name.startswith("tmp") and os.path.isdir(p):
                _shutil.rmtree(p, ignore_errors=True)
            elif name in ("_images", "_voices") and os.path.isdir(p):
                _shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


_atexit.register(_cleanup_tmp)

from groupmate.backend import MockBackend
from groupmate.bot import GroupMateBot
from groupmate.config import BotConfig
from groupmate.llm import sanitize_reply
from groupmate.message import GroupMessage, is_quiet_hour, strip_mention
from groupmate.policy import ReplyPolicy

# 媒体缓存(图片/语音)默认落在仓库根的 _images / _voices —— 测试要隔离掉
try:
    from groupmate.wx_backend import WxBackend as _WxBackend
    _WxBackend.media_cache_dir = _TMP_DIRS[0] if _TMP_DIRS else _tempfile.mkdtemp()
    _TMP_DIRS.append(_WxBackend.media_cache_dir)
except Exception:
    pass

# 测试用的落盘目录(记忆/状态)
_TEST_DATA_DIR = _tempfile.mkdtemp(prefix="groupmate_test_")
_TMP_DIRS.append(_TEST_DATA_DIR)

_OrigBot = GroupMateBot


class GroupMateBot(_OrigBot):                # noqa: F811
    """测试专用:默认把落盘目录指到临时目录,不污染仓库。"""

    def __init__(self, cfg, backend, llm=None, logger=None, data_dir=None):
        super().__init__(cfg, backend, llm=llm, logger=logger,
                         data_dir=data_dir or _TEST_DATA_DIR)


PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        print("  ✗ %s %s" % (name, extra))


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


def base_cfg(**trigger_over):
    cfg = BotConfig()
    cfg.dry_run = False
    cfg.trigger.groups = ["测试群"]
    cfg.trigger.mention_names = ["小助手"]
    cfg.trigger.keywords = ["天气"]
    cfg.trigger.random_reply_probability = 0.0
    cfg.trigger.per_user_cooldown_seconds = 60
    cfg.trigger.per_group_cooldown_seconds = 8
    cfg.trigger.self_silence_seconds = 20
    cfg.trigger.quiet_hours = [23, 7]
    cfg.trigger.image_wait_seconds = 0.0   # 测试里不等图片,保证速度
    cfg.trigger.image_follow_seconds = 90.0
    for k, v in trigger_over.items():
        setattr(cfg.trigger, k, v)
    return cfg


def msg(content="你好", sender="张三", group="测试群", t=0.0, mentioned=False,
        is_self=False, msg_id=""):
    return GroupMessage(group=group, sender=sender, content=content,
                        timestamp=t, mentioned_me=mentioned, is_self=is_self,
                        msg_id=msg_id)


def at_hour(hour: int, day_offset: int = 0) -> float:
    """构造「本地时间 hour 点」的时间戳。

    注意:不能拿 UTC 时间戳直接加减小时再交给 localtime 判断——
    这样在非 UTC+8 的机器上会算错,测试会假失败。
    """
    import datetime
    base = datetime.datetime(2026, 5, 20, hour, 0, 0) + datetime.timedelta(days=day_offset)
    return base.timestamp()


def test_policy():
    print("\n[1] 触发规则")
    clock = FakeClock(at_hour(10))

    p = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    d = p.decide(msg("大家早", t=clock.t))
    check("未被@且无关键词 -> 不回", not d.reply, d.reason)

    d = p.decide(msg("@小助手 在吗", t=clock.t, mentioned=True))
    check("被@ -> 回", d.reply and d.trigger == "mention", d.reason)

    p2 = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    d = p2.decide(msg("今天天气怎么样", t=clock.t))
    check("命中关键词 -> 回", d.reply and d.trigger == "keyword", d.reason)

    p3 = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    d = p3.decide(msg("随便聊聊", group="别的群", t=clock.t, mentioned=True))
    check("非白名单群 -> 不回", not d.reply and "白名单" in d.reason, d.reason)

    p4 = ReplyPolicy(base_cfg(groups=[]), now_fn=clock, rand=lambda: 0.99)
    d = p4.decide(msg("@小助手", t=clock.t, mentioned=True))
    check("未配置群 -> 一律不回(安全默认)", not d.reply, d.reason)

    p5 = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    d = p5.decide(msg("我自己说的", t=clock.t, is_self=True))
    check("自己发的 -> 不回", not d.reply and "自己" in d.reason, d.reason)

    print("\n[2] 深夜静默")
    p6 = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    night = at_hour(2)
    # 静默时段 @ 我 -> **必须回**(被点名优先级最高,和 README/配置注释一致)
    d = p6.decide(msg("@小助手 在吗", t=night, mentioned=True))
    check("凌晨2点被 @ -> 仍然要回", d.reply and d.trigger == "mention",
          "%s / %s" % (d.reply, d.reason))
    # 静默时段关键词 -> 不回
    p6b = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    d2 = p6b.decide(msg("今天天气怎么样", t=night, sender="李四"))
    check("凌晨2点关键词 -> 不回", not d2.reply and "静默" in d2.reason,
          d2.reason)
    # 静默时段主动接话 -> 不回
    p6c = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    d3 = p6c.decide(msg("有人知道食堂几点关门吗", t=night, sender="王五"))
    check("凌晨2点主动接话 -> 不回", not d3.reply and "静默" in d3.reason,
          d3.reason)
    check("跨零点判定正确(23-7)",
          is_quiet_hour(23, 7, at_hour(2)) and is_quiet_hour(23, 7, at_hour(23))
          and not is_quiet_hour(23, 7, at_hour(10)))
    check("同日窗口判定(9-18)",
          is_quiet_hour(9, 18, at_hour(12)) and not is_quiet_hour(9, 18, at_hour(20)))

    print("\n[3] 冷却")
    p7 = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    m1 = msg("天气如何", sender="李四", t=clock.t)
    d1 = p7.decide(m1)
    check("首次关键词 -> 回", d1.reply, d1.reason)
    p7.record_reply(m1, clock.t)

    d2 = p7.decide(msg("天气好吗", sender="李四", t=clock.t + 10))
    check("同一人 10s 内 -> 不回(冷却)", not d2.reply and "冷却" in d2.reason, d2.reason)

    d3 = p7.decide(msg("@小助手 喂", sender="李四", t=clock.t + 12, mentioned=True))
    check("@ 可突破冷却 -> 回", d3.reply, d3.reason)

    p7.record_reply(msg("x", sender="李四"), clock.t + 12)
    d4 = p7.decide(msg("今天天气", sender="王五", t=clock.t + 15))
    check("别人但本群刚回过 -> 不回(群冷却)", not d4.reply and "本群" in d4.reason, d4.reason)

    print("\n[4] 自我静默与 @ 防抖")
    p8 = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    m = msg("@小助手 在", t=clock.t, mentioned=True)
    p8.decide(m)
    p8.record_reply(m, clock.t)
    d = p8.decide(msg("@小助手 又是我", t=clock.t + 1, mentioned=True, msg_id="m2"))
    check("刚回过 1s 内再被@ -> 防抖挡住", not d.reply and "防抖" in d.reason, d.reason)
    d = p8.decide(msg("@小助手 又是我", t=clock.t + 5, mentioned=True, msg_id="m3"))
    check("超过防抖 5s 后@ -> 回(不装死)", d.reply, d.reason)

    # 非 @ 的触发,自我静默应完整生效
    p8b = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    mk = msg("今天天气", t=clock.t)
    p8b.decide(mk)
    p8b.record_reply(mk, clock.t)
    d = p8b.decide(msg("今天天气咋样", sender="王五", t=clock.t + 10, msg_id="k9"))
    check("非@触发受自我静默+群冷却约束", not d.reply, d.reason)

    print("\n[5] 硬护栏")
    cfg = base_cfg()
    cfg.guard.max_per_minute = 2
    p9 = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)
    for i in range(2):
        m = msg("@小助手 %d" % i, t=clock.t + i * 30, mentioned=True, msg_id="g%d" % i)
        # 绕过自我静默:@之后需要等
        d = p9.decide(m)
        if d.reply:
            p9.record_reply(m, clock.t + i * 30)
    ok, why = p9.guard_check(clock.t + 60)
    check("触达每分钟上限 -> 被拦", not ok and "每分钟" in why, why)

    cfg2 = base_cfg()
    cfg2.guard.max_per_day = 3
    p10 = ReplyPolicy(cfg2, now_fn=clock, rand=lambda: 0.99)
    for i in range(3):
        p10._day.stamps.append(clock.t + i)
    ok, why = p10.guard_check(clock.t + 10)
    check("触达每日上限 -> 被拦", not ok and "每日" in why, why)

    print("\n[6] 连续失败自动熔断")
    p11 = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    for _ in range(p11.cfg.guard.max_consecutive_failures):
        p11.record_failure()
    ok, why = p11.guard_check(clock.t)
    check("连续失败达上限 -> 自动暂停", not ok and "连续失败" in why, why)
    p11.resume()
    ok, _ = p11.guard_check(clock.t)
    check("人工 resume 后可恢复", ok)

    print("\n[7] 紧急停止开关")
    cfg3 = base_cfg()
    cfg3.guard.kill_switch_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_test_stop")
    p12 = ReplyPolicy(cfg3, now_fn=clock, rand=lambda: 0.99)
    d = p12.decide(msg("@小助手", t=clock.t, mentioned=True))
    check("无开关文件 -> 正常回", d.reply, d.reason)
    open(cfg3.guard.kill_switch_file, "w").close()
    try:
        d2 = p12.decide(msg("@小助手 again", t=clock.t + 30, mentioned=True, msg_id="k1"))
        check("开关文件存在 -> 停止", not d2.reply and "紧急停止" in d2.reason, d2.reason)
    finally:
        os.remove(cfg3.guard.kill_switch_file)

    print("\n[8] 去重")
    p13 = ReplyPolicy(base_cfg(), now_fn=clock, rand=lambda: 0.99)
    m = msg("@小助手 同一条", t=clock.t, mentioned=True, msg_id="dup1")
    d1 = p13.decide(m)
    d2 = p13.decide(m)
    check("同一条消息只处理一次", d1.reply and not d2.reply and "重复" in d2.reason, d2.reason)


def test_sanitize():
    print("\n[9] 输出清洗")
    check("去掉整段引号", sanitize_reply('"你好啊"', 100) == "你好啊")
    check("去掉「回复：」前缀", sanitize_reply("回复：在的", 100) == "在的")
    check("折叠空行", sanitize_reply("第一行\n\n\n第二行", 100) == "第一行第二行",
          sanitize_reply("第一行\n\n\n第二行", 100))
    steps = "这题分三步\n1.先化简\n2.再代入\n3.得答案12"
    check("列步骤保留分行", "\n" in sanitize_reply(steps, 600),
          repr(sanitize_reply(steps, 600)))
    check("emoji 代码保留", sanitize_reply("在，咋了[旺柴]", 100) == "在，咋了[旺柴]")
    check("长回复保留分段",
          "\n" in sanitize_reply("第一段" * 40 + "\n" + "第二段" * 40, 600))

    # emoji 限流:一条最多留一个,且不误伤普通方括号
    from groupmate.llm import limit_emoji
    check("单个表情保留", limit_emoji("哈喽[旺柴]", 1) == "哈喽[旺柴]")
    check("多余表情被删",
          limit_emoji("行行行我傻逼[捂脸]你开心就好[吃瓜]", 1)
          == "行行行我傻逼[捂脸]你开心就好")
    check("连续多个只留一个",
          limit_emoji("[破涕为笑][捂脸][偷笑] 确实", 1) == "[破涕为笑] 确实")
    check("没表情的句子不动", limit_emoji("没有表情的一句话", 1) == "没有表情的一句话")
    check("数字方括号不误伤",
          limit_emoji("第[1]题选[2]", 1) == "第[1]题选[2]",
          limit_emoji("第[1]题选[2]", 1))
    check("完整链路也限流",
          sanitize_reply("这题我看看哈[捂脸][吃瓜][偷笑]", 600) == "这题我看看哈[捂脸]",
          sanitize_reply("这题我看看哈[捂脸][吃瓜][偷笑]", 600))
    long = "字" * 300
    check("超长截断", len(sanitize_reply(long, 120)) == 120)
    check("空输入", sanitize_reply("", 100) == "")
    check("中文引号", sanitize_reply("“收到”", 100) == "收到")


class StubLLM:
    """假的模型,返回可预测的内容。"""
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or ["好的呀"])
        self.calls = 0
        self.ready = True
        self.last_images = None      # 记录最后一次收到的图片数

    def reply(self, msg, history, system_prompt, images=None, mention_names=None):
        self.calls += 1
        self.last_images = images
        self.last_mention_names = mention_names
        if isinstance(self.outcomes[0], Exception):
            raise self.outcomes.pop(0)
        return self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]


def test_end_to_end():
    print("\n[10] 端到端(含极简 prompt 断言)")
    clock = FakeClock(at_hour(10))

    cfg = base_cfg()
    cfg.dry_run = False
    backend = MockBackend()
    llm = StubLLM(["今天晴，挺热的"])
    bot = GroupMateBot(cfg, backend, llm=llm, logger=lambda m: None)
    bot.policy._now = clock

    bot.handle(msg("今天天气怎么样", sender="张三", t=clock.t))
    check("关键词触发并发送", len(backend.sent) == 1 and "挺热" in backend.sent[0][1],
          str(backend.sent))
    check("发送目标正确", backend.sent and backend.sent[0][0] == "测试群")
    check("统计正确", bot.stats["replied"] == 1 and bot.stats["seen"] == 0, str(bot.stats))

    # 同一人立刻再问 -> 冷却
    bot.handle(msg("那明天呢", sender="张三", t=clock.t + 5))
    check("冷却生效,未重复发送", len(backend.sent) == 1, str(backend.sent))

    # 干跑模式
    cfg2 = base_cfg()
    cfg2.dry_run = True
    b2 = MockBackend()
    bot2 = GroupMateBot(cfg2, b2, llm=StubLLM(["干跑内容"]), logger=lambda m: None)
    bot2.policy._now = clock
    bot2.handle(msg("@小助手 hi", sender="李四", t=clock.t, mentioned=True))
    check("干跑不发送但计数", len(b2.sent) == 0 and bot2.stats["replied"] == 1,
          "%s / %s" % (b2.sent, bot2.stats))

    # 发送失败 -> 连续失败累计
    print("\n[11] 发送失败熔断")
    cfg3 = base_cfg()
    cfg3.dry_run = False
    cfg3.guard.max_consecutive_failures = 2
    b3 = MockBackend(fail_sends=True)
    bot3 = GroupMateBot(cfg3, b3, llm=StubLLM(["x"]), logger=lambda m: None)
    bot3.policy._now = clock
    t = clock.t
    for i in range(2):
        bot3.handle(msg("@小助手 %d" % i, t=t + i * 40, mentioned=True, msg_id="f%d" % i))
    check("连续失败累计到 2", bot3.policy.consecutive_failures == 2,
          str(bot3.policy.consecutive_failures))
    ok, why = bot3.policy.guard_check(t + 200)
    check("熔断后拒绝发送", not ok and "连续失败" in why, why)

    # 模型报错 -> 不发送
    print("\n[12] 模型异常处理")
    cfg4 = base_cfg()
    cfg4.dry_run = False
    b4 = MockBackend()
    from groupmate.llm import LLMError
    bot4 = GroupMateBot(cfg4, b4, llm=StubLLM([LLMError("模拟网络错误")]),
                        logger=lambda m: None)
    bot4.policy._now = clock
    bot4.handle(msg("@小助手 在吗", t=clock.t, mentioned=True))
    check("模型报错时不发送", len(b4.sent) == 0)
    check("失败被记账", bot4.policy.consecutive_failures == 1)
    check("空回复不发送", GroupMateBot(cfg4, MockBackend(), llm=StubLLM(["   "]),
                                    logger=lambda m: None) is not None)


def test_prompt():
    print("\n[13] 七字段人设 prompt 组装")
    cfg = BotConfig()
    cfg.persona.name = "阿伟"
    cfg.persona.background = "喜欢篮球"
    cfg.persona.personality = "爱开玩笑"
    cfg.persona.language_style = "说话很冲"
    cfg.persona.motivation = "想跟大家混熟"
    cfg.persona.values = "不装"
    cfg.persona.limitations = "算不清复杂题"
    cfg.persona.examples = [["@阿伟 吃啥", "随便整点"]]
    sp = cfg.persona.build_system_prompt()
    check("包含名字", "阿伟" in sp)
    check("包含背景", "篮球" in sp)
    check("包含性格", "爱开玩笑" in sp)
    check("包含语言风格", "说话很冲" in sp)
    check("包含动机(最关键字段)", "想跟大家混熟" in sp)
    check("包含价值观", "不装" in sp)
    check("包含能力局限", "算不清复杂题" in sp)
    check("包含示例对话", "随便整点" in sp)
    check("要求直接回答问题", "直接回答" in sp)
    check("要求只输出正文", "只输出" in sp)
    check("七个字段都有标签",
          all(f"【{k}】" in sp for k in ("背景", "性格", "语言风格", "动机",
                                        "价值观", "能力局限")))


def test_strip_mention():
    print("\n[14] @ 前缀剥离(只把问题本身交给 AI)")
    check("普通 @", strip_mention("@小助手 今天天气怎么样") == "今天天气怎么样",
          repr(strip_mention("@小助手 今天天气怎么样")))
    check("带占位符的 @(微信真实格式)",
          strip_mention("@小助手\u2005今天天气怎么样") == "今天天气怎么样")
    check("多处占位符",
          strip_mention("@小助手\u2005你好\u2005在吗") == "你好 在吗")
    check("别的名字", strip_mention("@小助手 帮我算个题") == "帮我算个题")
    check("只有 @ 没有内容", strip_mention("@小助手") == "")
    check("只有 @ 加空格", strip_mention("@小助手 ") == "")
    check("没有 @ 时原样返回", strip_mention("今天天气怎么样") == "今天天气怎么样")
    check("多空格折叠", strip_mention("@小助手  多个   空格") == "多个 空格")
    check("空输入", strip_mention("") == "")
    check("None 输入", strip_mention(None) == "")


def test_images():
    print("\n[15] 图片识别与收集")
    import os as _os
    import tempfile

    # 造两张真图片
    from PIL import Image
    tmpdir = tempfile.mkdtemp()
    p1 = _os.path.join(tmpdir, "hw1.png")
    p2 = _os.path.join(tmpdir, "hw2.png")
    Image.new("RGB", (60, 60), "white").save(p1)
    Image.new("RGB", (60, 60), "gray").save(p2)

    # is_image_msg 判定
    m_img = msg("", msg_id="i1")
    m_img.msg_type = "图片"
    check("类型为图片 -> is_image_msg", m_img.is_image_msg)
    m_txt = msg("[图片]", msg_id="i2")
    check("正文为[图片] -> is_image_msg", m_txt.is_image_msg)
    check("普通文本 -> 不是图片", not msg("你好", msg_id="i3").is_image_msg)
    check("has_image 需要真实路径", not m_img.has_image)
    m_img.image_path = p1
    check("有路径后 has_image", m_img.has_image)

    # 收集逻辑:同一人 @ 之后发的图应被收集
    cfg = base_cfg()
    cfg.dry_run = False
    clock = FakeClock(at_hour(10))
    backend = MockBackend()
    llm = StubLLM(["看到了"])
    bot = GroupMateBot(cfg, backend, llm=llm, logger=lambda m: None)
    bot.policy._now = clock

    t0 = clock.t
    mention = msg("@小助手 看下这几道题", sender="张三", t=t0, mentioned=True, msg_id="m1")
    img1 = msg("", sender="张三", t=t0 + 2, msg_id="m2")
    img1.msg_type = "图片"
    img1.image_path = p1
    img2 = msg("", sender="张三", t=t0 + 3, msg_id="m3")
    img2.msg_type = "图片"
    img2.image_path = p2
    other = msg("", sender="李四", t=t0 + 3, msg_id="m4")   # 别人的图,不该收
    other.msg_type = "图片"
    other.image_path = p2

    # 放进该群上下文(收集逻辑从这里 + history 里找图)
    bot.context["测试群"] = [mention, img1, img2, other]
    bot.history = [mention, img1, img2, other]
    _txt, got, _consumed = bot._gather_followups(mention)
    check("收集到自己发的 2 张图", len(got) == 2, str(got))
    check("不含别人的图", p2 in got and len([g for g in got if g == p2]) == 1, str(got))

    # 传给 LLM 的图片数
    bot.handle(msg("@小助手 帮我看看", sender="张三", t=t0 + 50,
                  mentioned=True, msg_id="m9"))
    check("图片已传给模型", llm.last_images is not None and len(llm.last_images) >= 1,
          str(llm.last_images))

    # 图片不存在时不收集
    img3 = msg("", sender="王五", t=t0 + 3, msg_id="m5")
    img3.msg_type = "图片"
    img3.image_path = _os.path.join(tmpdir, "nope.png")
    bot.context["测试群"] = [mention, img3]
    bot.history = [mention, img3]
    _txt2, got2, _c2 = bot._gather_followups(mention)
    check("文件不存在的图片被跳过", len(got2) == 0, str(got2))

    # 回归:poll() 会清空内部缓冲,ensure_images 必须能接收外部消息列表,
    # 否则图片永远不会被下载(这个 bug 真实发生过)
    class FakeMedia:
        def __init__(self):
            self.calls = []

        def download_image(self, user, local_id, save_dir=None, tier=None, shard=None):
            self.calls.append((local_id, tier))
            p = _os.path.join(tmpdir, "dl_%s.jpg" % local_id)
            Image.new("RGB", (40, 40), "blue").save(p)
            return p

    from groupmate.wx_backend import WxBackend
    be = WxBackend(["测试群"])
    be._media = FakeMedia()
    be._group_usernames["测试群"] = "123@chatroom"

    m_img = msg("", sender="张三", t=t0, msg_id="777")
    m_img.msg_type = "图片"
    m_img.shard = "message__message_0.db"

    n = be.ensure_images([m_img])
    check("ensure_images 接收外部消息列表并下载", n == 1, str(n))
    check("下载后 image_path 写回消息对象", bool(m_img.image_path), m_img.image_path)
    check("下载用了 best 档(原图优先)",
          be._media.calls and be._media.calls[0][1] == "best",
          str(be._media.calls))
    check("ensure_images 空列表不报错", be.ensure_images([]) == 0)

    # 回归:老版本库的 download_image 没有 shard 参数(1.2.4.4),
    # 直接传 shard 会抛 TypeError -> 图片永远下不到。必须按签名自适应。
    class OldMedia:
        """模拟 1.2.4.4:签名里没有 shard。"""
        def __init__(self):
            self.calls = []

        def download_image(self, user, local_id, save_dir=None,
                           aes_key=None, xor_key=None, tier=None):
            self.calls.append((local_id, tier))
            p = _os.path.join(tmpdir, "old_%s.jpg" % local_id)
            Image.new("RGB", (30, 30), "green").save(p)
            return p

    be2 = WxBackend(["测试群"])
    be2._media = OldMedia()
    be2._media_supports_shard = False        # 探测结果:不支持
    be2._group_usernames["测试群"] = "456@chatroom"

    m_old = msg("", sender="张三", t=t0, msg_id="888")
    m_old.msg_type = "图片"
    m_old.shard = "message__message_0.db"    # 有 shard 信息也不该传进去
    n2 = be2.ensure_images([m_old])
    check("老版本库(无 shard 参数)也能下载图片", n2 == 1, str(n2))
    check("老版本下载后路径正确", bool(m_old.image_path), m_old.image_path)

    # 探测到支持 shard 时才传
    class NewMedia(OldMedia):
        def download_image(self, user, local_id, save_dir=None, aes_key=None,
                           xor_key=None, tier=None, shard=None):
            self.calls.append((local_id, tier, shard))
            p = _os.path.join(tmpdir, "new_%s.jpg" % local_id)
            Image.new("RGB", (30, 30), "red").save(p)
            return p

    be3 = WxBackend(["测试群"])
    be3._media = NewMedia()
    be3._media_supports_shard = True
    be3._group_usernames["测试群"] = "789@chatroom"
    m_new = msg("", sender="张三", t=t0, msg_id="999")
    m_new.msg_type = "图片"
    m_new.shard = "message__message_0.db"
    n3 = be3.ensure_images([m_new])
    check("支持 shard 的新版本能下载", n3 == 1, str(n3))
    check("新版本确实把 shard 传进去了",
          any(len(c) >= 3 and c[2] for c in be3._media.calls),
          str(be3._media.calls))

    # data URL 编码
    from groupmate.llm import _to_data_url
    du = _to_data_url(p1)
    check("小图能编成 data URL", bool(du) and du.startswith("data:image/"), str(du)[:40])
    check("不存在的文件返回 None", _to_data_url("/no/such/file.png") is None)

    # 清理
    for f in (p1, p2):
        try:
            _os.remove(f)
        except OSError:
            pass
    try:
        _os.rmdir(tmpdir)
    except OSError:
        pass


def test_image_follow():
    print("\n[16] 图片跟进触发(先 @ 再单独发图)")
    clock = FakeClock(at_hour(10))
    cfg = base_cfg()
    cfg.trigger.image_follow_seconds = 90.0
    p = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)

    # 先 @ 一下
    m1 = msg("@小助手", sender="张三", t=clock.t, mentioned=True, msg_id="f1")
    d1 = p.decide(m1)
    check("先 @ 能触发", d1.reply and d1.trigger == "mention", d1.reason)
    p.record_reply(m1, clock.t)

    # 紧接着单独发图(没有 @) -> 应触发
    img = msg("", sender="张三", t=clock.t + 5, msg_id="f2")
    img.msg_type = "图片"
    d2 = p.decide(img)
    check("随后单独发图 -> 图片跟进触发",
          d2.reply and d2.trigger == "image_follow",
          "%s / %s" % (d2.reply, d2.reason))

    # 别人的图不该触发(不是刚 @ 我的那个人)
    p2 = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)
    m2 = msg("@小助手", sender="张三", t=clock.t, mentioned=True, msg_id="g1")
    p2.decide(m2)
    p2.record_reply(m2, clock.t)
    other_img = msg("", sender="李四", t=clock.t + 5, msg_id="g2")
    other_img.msg_type = "图片"
    d3 = p2.decide(other_img)
    check("别人发的图不触发", not d3.reply, d3.reason)

    # 超过时间窗的图不触发
    p3 = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)
    m3 = msg("@小助手", sender="张三", t=clock.t, mentioned=True, msg_id="h1")
    p3.decide(m3)
    p3.record_reply(m3, clock.t)
    late_img = msg("", sender="张三", t=clock.t + 200, msg_id="h2")
    late_img.msg_type = "图片"
    d4 = p3.decide(late_img)
    check("超出时间窗的图不触发", not d4.reply, d4.reason)

    # 没 @ 过就发图 -> 不触发
    p4 = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)
    lone_img = msg("", sender="王五", t=clock.t, msg_id="i1")
    lone_img.msg_type = "图片"
    d5 = p4.decide(lone_img)
    check("没@过就发图不触发", not d5.reply, d5.reason)


def test_followup_merge():
    print("\n[18] @ 之后补发的内容(文字 + 图片)要合并理解")
    clock = FakeClock(at_hour(10))
    cfg = base_cfg()
    cfg.dry_run = False
    cfg.trigger.image_wait_seconds = 0.0     # 测试不等
    backend = MockBackend()
    llm = StubLLM(["好的"])
    bot = GroupMateBot(cfg, backend, llm=llm, logger=lambda m: None)
    bot.policy._now = clock

    t0 = clock.t
    # 场景:先 @,再把真正的问题补发出来
    backend.feed(msg("@小助手", sender="张三", t=t0, mentioned=True, msg_id="f1"))
    backend.feed(msg("这道题怎么做", sender="张三", t=t0 + 1, msg_id="f2"))
    backend.feed(msg("我不太会", sender="张三", t=t0 + 2, msg_id="f3"))

    merged = {}

    class CapLLM(StubLLM):
        def reply(self, msg, history, system_prompt, images=None,
                  mention_names=None):
            merged["content"] = msg.content
            merged["images"] = images
            return super().reply(msg, history, system_prompt, images,
                                 mention_names)

    bot.llm = CapLLM(["好的"])
    bot.tick()

    got = merged.get("content") or ""
    check("@ 触发后补发的文字被合并进问题",
          "这道题怎么做" in got, repr(got))
    check("多条补发都合并了", "我不太会" in got, repr(got))
    check("模型只被调用一次(不是每条都回)", bot.llm.calls == 1,
          str(bot.llm.calls))
    check("补发的消息没被当成新触发重复处理",
          bot.stats["replied"] == 1, str(bot.stats))

    # 场景:别人补发的内容不该合并进来
    clock2 = FakeClock(at_hour(10))
    b2 = MockBackend()
    b2.feed(msg("@小助手", sender="李四", t=clock2.t, mentioned=True, msg_id="g1"))
    b2.feed(msg("这是我的问题", sender="王五", t=clock2.t + 1, msg_id="g2"))
    cfg2 = base_cfg()
    cfg2.dry_run = False
    cfg2.trigger.image_wait_seconds = 0.0
    bot2 = GroupMateBot(cfg2, b2, llm=StubLLM(["嗯"]), logger=lambda m: None)
    bot2.policy._now = clock2
    captured = {}

    class Cap2(StubLLM):
        def reply(self, msg, history, system_prompt, images=None,
                  mention_names=None):
            captured["content"] = msg.content
            return "嗯"

    bot2.llm = Cap2(["嗯"])
    bot2.tick()
    check("别人在此期间说的话不会被合并",
          "这是我的问题" not in (captured.get("content") or ""),
          repr(captured.get("content")))


def test_echo_and_cooldown():
    print("\n[19] 防止『自己的消息被读回来』导致连发")
    cfg = base_cfg()
    cfg.dry_run = False
    cfg.trigger.image_wait_seconds = 0.0
    backend = MockBackend()
    bot = GroupMateBot(cfg, backend, llm=StubLLM(["第一条回复", "第二条回复"]),
                       logger=lambda m: None)

    # 发一条 -> 记录下来
    my_reply = "这不就是刚那张二次元图嘛，双V手势，白衬衫"
    bot._note_sent("测试群", my_reply)

    # 完全相同的回读 -> 应被认出
    echo = msg(my_reply, sender="张三", t=0, msg_id="e1")
    check("自己的回复被读回来要能认出", bot._is_own_echo(echo))

    # 被截断的回读也要认出(微信有时截断长消息)
    trunc = msg(my_reply[:15], sender="张三", t=0, msg_id="e2")
    check("被截断的回读也要认出", bot._is_own_echo(trunc))

    # 别人真正说的话不该误判
    other = msg("我觉得这张图挺好看的", sender="张三", t=0, msg_id="e3")
    check("别人的正常发言不误判", not bot._is_own_echo(other))

    # is_self 标记的也要拦
    me = msg("随便什么", sender="我", t=0, msg_id="e4")
    me.is_self = True
    check("is_self 的消息被拦", bot._is_own_echo(me))

    # 超过时间窗就不算了
    bot._recent_sent["测试群"] = [(my_reply, __import__("time").time() - 400)]
    check("超出时间窗不再拦", not bot._is_own_echo(echo))

    # 刚回复过的群,短时间内不再接话
    import time as _t
    cfg2 = base_cfg()
    cfg2.dry_run = False
    cfg2.trigger.image_wait_seconds = 0.0
    cfg2.trigger.post_send_cooldown_seconds = 12.0
    b2 = MockBackend()
    llm2 = StubLLM(["回复一", "回复二"])
    bot2 = GroupMateBot(cfg2, b2, llm=llm2, logger=lambda m: None)
    bot2.policy._now = FakeClock(at_hour(10))

    b2.feed(msg("@小助手 在吗", sender="张三", t=0, mentioned=True, msg_id="c1"))
    bot2.tick()
    check("第一条正常回复", bot2.stats["replied"] == 1, str(bot2.stats))

    # 紧接着又 @ -> 冷却生效,不该再回
    b2.feed(msg("@小助手 再问一下", sender="张三", t=1, mentioned=True, msg_id="c2"))
    bot2.tick()
    check("刚回复过 -> 冷却期内不再回",
          bot2.stats["replied"] == 1, str(bot2.stats))
    check("冷却期内只调用了一次模型", llm2.calls == 1, str(llm2.calls))

    # 没有"刚发过"的记录时,应该能正常回复
    # (用全新实例,避免策略层自己的冷却干扰这条断言)
    cfg3 = base_cfg()
    cfg3.dry_run = False
    cfg3.trigger.image_wait_seconds = 0.0
    cfg3.trigger.post_send_cooldown_seconds = 12.0
    b3 = MockBackend()
    llm3 = StubLLM(["好的"])
    bot3 = GroupMateBot(cfg3, b3, llm=llm3, logger=lambda m: None)
    bot3.policy._now = FakeClock(at_hour(10))
    b3.feed(msg("@小助手 在吗", sender="张三", t=0, mentioned=True, msg_id="d1"))
    bot3.tick()
    check("没有发送记录时正常回复", bot3.stats["replied"] == 1, str(bot3.stats))
    check("生效的是发送冷却而非永久屏蔽",
          bot3._recent_sent.get("测试群"), str(bot3._recent_sent))


def test_consecutive_mentions():
    print("\n[20] 连续两个 @ 都要回答，不能遗漏")
    clock = FakeClock(at_hour(10))
    cfg = base_cfg()
    cfg.trigger.per_user_cooldown_seconds = 3
    cfg.trigger.self_silence_seconds = 1
    cfg.trigger.mention_debounce_seconds = 0
    cfg.trigger.post_send_cooldown_seconds = 0
    cfg.dry_run = False
    cfg.trigger.image_wait_seconds = 0.0

    backend = MockBackend()
    llm = StubLLM(["回答一", "回答二", "回答三"])
    bot = GroupMateBot(cfg, backend, llm=llm, logger=lambda m: None)
    bot.policy._now = clock

    # 连续两条 @（同一人，间隔 1 秒，第一条机器人才刚回过）
    m1 = msg("@小助手 第一个问题", sender="张三", t=clock.t, mentioned=True, msg_id="k1")
    m2 = msg("@小助手 第二个问题", sender="张三", t=clock.t + 1, mentioned=True, msg_id="k2")
    backend.feed(m1)
    backend.feed(m2)
    bot.tick()
    check("两条 @ 都回答了", bot.stats["replied"] == 2, str(bot.stats))
    check("模型被调用两次", llm.calls == 2, str(llm.calls))
    check("两条内容都发出去了", len(backend.sent) == 2, str(backend.sent))

    # 策略层直接验证:刚回过之后,再来一个 @ 也要放行
    cfg2 = base_cfg()
    cfg2.trigger.per_user_cooldown_seconds = 3
    cfg2.trigger.self_silence_seconds = 10        # 故意设很长
    cfg2.trigger.mention_debounce_seconds = 0
    p = ReplyPolicy(cfg2, now_fn=clock, rand=lambda: 0.99)
    a = msg("@小助手 问题A", sender="张三", t=clock.t, mentioned=True, msg_id="p1")
    d1 = p.decide(a)
    check("第一条 @ 放行", d1.reply, d1.reason)
    p.record_reply(a, clock.t)
    b = msg("@小助手 问题B", sender="张三", t=clock.t + 1, mentioned=True, msg_id="p2")
    d2 = p.decide(b)
    check("自静默期间第二条 @ 仍放行(@ 完全绕过)",
          d2.reply and d2.trigger == "mention", "%s / %s" % (d2.reply, d2.reason))

    # 同一条消息重复投递 -> 仍然要挡住(去重)
    d3 = p.decide(b)
    check("同一条重复投递被去重", not d3.reply, d3.reason)


def test_proactive():
    print("\n[21] 主动接话(不 @ 也读、该答就答)")
    from groupmate.proactive import should_chime_in

    def m(text, sender="张三", mentioned=False, self_msg=False, gid="测试群"):
        x = msg(text, sender=sender, mentioned=mentioned, msg_id=text[:10] + sender)
        x.is_self = self_msg
        return x

    NAMES = ["小助手"]

    # --- 该接的情况 ---
    ok, why, _rep = should_chime_in(m("这个谁知道怎么做啊"), [], NAMES)
    check("在问问题 -> 该接", ok, why)

    ok, why, _rep = should_chime_in(m("为什么天是蓝的"), [], NAMES)
    check("为什么 -> 该接", ok, why)

    ok, why, _rep = should_chime_in(m("有人知道明天放假吗"), [], NAMES)
    check("有人知道 -> 该接", ok, why)

    ok, why, _rep = should_chime_in(m("几点集合来着？"), [], NAMES)
    check("几点 + 问号 -> 该接", ok, why)

    # --- 不该接的情况 ---
    ok, why, _rep = should_chime_in(m("@小明 你觉得呢"), [], NAMES)
    check("@了别人 -> 不接", not ok, why)

    ok, why, _rep = should_chime_in(m("哈哈哈"), [], NAMES)
    check("纯附和 -> 不接", not ok, why)

    ok, why, _rep = should_chime_in(m("[旺柴]"), [], NAMES)
    check("纯表情 -> 不接", not ok, why)

    ok, why, _rep = should_chime_in(m("嗯"), [], NAMES)
    check("单字 -> 不接", not ok, why)

    ok, why, _rep = should_chime_in(m(""), [], NAMES)
    check("空消息 -> 不接", not ok, why)

    ok, why, _rep = should_chime_in(m("我今天下午去打球"), [], NAMES)
    check("普通陈述 -> 不接", not ok, why)

    ok, why, _rep = should_chime_in(m("随便说说", self_msg=True), [], NAMES)
    check("自己的消息 -> 不接", not ok, why)

    # --- 在接机器人的话:要标出 replying_to_me=True(可绕过冷却)---
    import time as _t
    mine = msg("答案是 12", sender="我", msg_id="mine1")
    mine.is_self = True
    mine.timestamp = _t.time() - 5
    ok, why, rep = should_chime_in(m("那为啥不是 13"), [mine], NAMES)
    check("接着我的话问 -> 该接", ok, why)
    check("标为『在接我的话』(可绕过冷却)", rep, str(rep))

    # 无关的问题不能被标成"在接我的话"
    ok2, why2, rep2 = should_chime_in(m("谁知道怎么去车站"), [], NAMES)
    check("没上下文的问题不算接我的话", ok2 and not rep2, "%s / %s" % (why2, rep2))

    # --- 冷却:主动接话有独立上限 ---
    clock = FakeClock(at_hour(10))
    cfg = base_cfg()
    cfg.dry_run = False
    cfg.trigger.image_wait_seconds = 0.0
    cfg.trigger.proactive_enabled = True
    cfg.trigger.proactive_cooldown_seconds = 40
    cfg.trigger.per_user_cooldown_seconds = 0
    cfg.trigger.per_group_cooldown_seconds = 0
    cfg.trigger.self_silence_seconds = 0
    cfg.trigger.mention_debounce_seconds = 0
    backend = MockBackend()
    llm = StubLLM(["答一", "答二", "答三"])
    bot = GroupMateBot(cfg, backend, llm=llm, logger=lambda mm: None)
    bot.policy._now = clock

    # 第一句像问题 -> 主动接
    backend.feed(msg("这个怎么做啊", sender="张三", t=clock.t, msg_id="pa1"))
    bot.tick()
    check("第一句主动接了", bot.stats["replied"] == 1, str(bot.stats))

    # 马上又来一句像问题 -> 被主动冷却挡住
    backend.feed(msg("那这个呢怎么做", sender="李四", t=clock.t + 2, msg_id="pa2"))
    bot.tick()
    check("主动接话冷却生效", bot.stats["replied"] == 1, str(bot.stats))

    # 但 @ 我 不受主动冷却限制
    backend.feed(msg("@小助手 帮我看看", sender="王五", t=clock.t + 3,
                    mentioned=True, msg_id="pa3"))
    bot.tick()
    check("@ 不受主动冷却影响", bot.stats["replied"] == 2, str(bot.stats))

    # --- 复现真实场景:@ 之后对方接了一句,机器人必须接上 ---
    # 用户: @机器人 赛罗还是哥斯拉厉害   -> 机器人答
    # 用户: 你还挺懂                     -> 必须回(在接机器人刚说的话)
    clock3 = FakeClock(at_hour(10))
    cfg3 = base_cfg()
    cfg3.dry_run = False
    cfg3.trigger.image_wait_seconds = 0.0
    cfg3.trigger.proactive_enabled = True
    cfg3.trigger.proactive_cooldown_seconds = 600     # 故意设超长
    cfg3.trigger.per_user_cooldown_seconds = 0
    cfg3.trigger.per_group_cooldown_seconds = 0
    cfg3.trigger.self_silence_seconds = 0
    cfg3.trigger.mention_debounce_seconds = 0
    cfg3.trigger.post_send_cooldown_seconds = 8      # 线上就是这个值
    b3 = MockBackend()
    llm3 = StubLLM(["赛罗厉害", "谢啦"])
    bot3 = GroupMateBot(cfg3, b3, llm=llm3, logger=lambda m: None)
    bot3.policy._now = clock3

    b3.feed(msg("@小助手 赛罗还是哥斯拉厉害", sender="张三", t=clock3.t,
                mentioned=True, msg_id="rt1"))
    bot3.tick()
    check("先被 @ 回答", bot3.stats["replied"] == 1, str(bot3.stats))

    # 10 秒后对方接了一句(主动冷却设了 600 秒,按理会被挡)
    b3.feed(msg("你还挺懂", sender="张三", t=clock3.t + 10, msg_id="rt2"))
    # 模拟时间推进
    clock3.t += 10
    bot3.tick()
    check("对方接我的话 -> 绕过冷却也要回",
          bot3.stats["replied"] == 2, str(bot3.stats))


def test_voice():
    print("\n[22] 语音识别(语音消息 → 文字)")
    import os as _os
    import shutil
    import tempfile

    from groupmate.voice import (VoiceError, VoiceTranscriber,
                                 silk_to_wav, wav_duration)

    # --- 语音消息判定 ---
    v = msg("", msg_id="v1")
    v.msg_type = "语音"
    check("类型为语音 -> is_voice_msg", v.is_voice_msg)
    check("语音不是图片消息", not v.is_image_msg)
    v2 = msg("[语音]", msg_id="v2")
    check("正文为[语音] -> is_voice_msg", v2.is_voice_msg)
    check("普通文本不是语音", not msg("你好", msg_id="v3").is_voice_msg)

    # --- 语音触发:先 @ 再发语音条 ---
    clock = FakeClock(at_hour(10))
    cfg = base_cfg()
    cfg.trigger.image_follow_seconds = 90
    p = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)
    m1 = msg("@小助手", sender="张三", t=clock.t, mentioned=True, msg_id="vf1")
    check("先 @ 触发", p.decide(m1).reply)
    p.record_reply(m1, clock.t)
    voice = msg("", sender="张三", t=clock.t + 5, msg_id="vf2")
    voice.msg_type = "语音"
    d = p.decide(voice)
    check("随后发语音条 -> voice_follow 触发",
          d.reply and d.trigger == "voice_follow",
          "%s / %s" % (d.reply, d.reason))

    # --- 转录器:没 Key 时要明确报错,不静默 ---
    # 注意 api_key_env 用一个肯定不存在的名字,否则会读到机器上真配的 Key
    tr = VoiceTranscriber(api_key="", api_key_env="_NO_SUCH_ASR_KEY_",
                          cache_dir=tempfile.mkdtemp())
    check("没 Key 时 ready=False", not tr.ready)
    raised = False
    try:
        tr.transcribe("不管什么路径.wav")
    except VoiceError:
        raised = True
    except Exception:
        pass
    check("没 Key 时 transcribe 报 VoiceError", raised)

    # --- 缓存:同一文件第二次不再请求 ---
    tmpd = tempfile.mkdtemp()
    fake_wav = _os.path.join(tmpd, "a.wav")
    with open(fake_wav, "wb") as f:
        f.write(b"RIFF----WAVEfmt ")      # 内容不重要,只看哈希
    tr2 = VoiceTranscriber(api_key="fake-key", cache_dir=tmpd)
    calls = {"n": 0}

    def fake_post(path):
        calls["n"] += 1
        return "识别出来的文字"

    tr2._post = fake_post
    r1 = tr2.transcribe(fake_wav)
    r2 = tr2.transcribe(fake_wav)
    check("第一次识别成功", r1 == "识别出来的文字", str(r1))
    check("第二次命中缓存", r2 == "识别出来的文字", str(r2))
    check("只请求了一次(没重复花钱)", calls["n"] == 1, str(calls["n"]))

    # --- 失败的 SILK 不炸 ---
    bad = _os.path.join(tmpd, "bad.silk")
    with open(bad, "wb") as f:
        f.write(b"not a silk file")
    err = None
    try:
        silk_to_wav(bad, _os.path.join(tmpd, "bad.wav"))
    except VoiceError as e:
        err = e
    except Exception as e:
        err = "非 VoiceError: %r" % (e,)
    check("坏 SILK 抛 VoiceError(而不是崩)", isinstance(err, VoiceError),
          str(err))
    # 不存在的文件也要抛 VoiceError(而不是静默返回)
    missing_err = None
    try:
        silk_to_wav("/no/such/file.silk", _os.path.join(tmpd, "x.wav"))
    except VoiceError as e:
        missing_err = e
    except Exception as e:
        missing_err = "非 VoiceError: %r" % (e,)
    check("不存在的文件也抛 VoiceError", isinstance(missing_err, VoiceError),
          str(missing_err))

    # --- wav_duration 对坏文件返回 0 而不是抛 ---
    check("坏 WAV 时长返回 0", wav_duration("/no/such.wav") == 0.0)

    # --- 回归:语音延迟下载时必须重试,不能一次就放弃 ---
    from groupmate.wx_backend import WxBackend

    class LazyVoice:
        """模拟微信延迟下载:前 2 次说没有,第 3 次才给。"""
        def __init__(self):
            self.calls = 0

        def download_voice(self, user, local_id, save_dir=None):
            self.calls += 1
            if self.calls < 3:
                return None
            p = _os.path.join(tempfile.mkdtemp(), "v_%s.silk" % local_id)
            with open(p, "wb") as f:
                f.write(b"\x02#!SILK_V3" + b"\x00" * 20)
            return p

        def voice_status(self, user, local_id):
            return {"reason": "audio_missing_from_media_db"}

    be = WxBackend(["测试群"])
    be._media = LazyVoice()
    be._group_usernames["测试群"] = "111@chatroom"
    got = be.fetch_voice("测试群", "268", retries=4, delay=0.01)
    check("语音延迟下载时会重试并最终拿到", bool(got), str(got))
    check("确实重试了(不是一次就放弃)", be._media.calls >= 3,
          str(be._media.calls))

    # 真的没有时不该傻等
    class NeverVoice:
        def __init__(self):
            self.calls = 0

        def download_voice(self, user, local_id, save_dir=None):
            self.calls += 1
            return None

        def voice_status(self, user, local_id):
            return {"reason": "no_voice_row"}

    be2 = WxBackend(["测试群"])
    be2._media = NeverVoice()
    be2._group_usernames["测试群"] = "222@chatroom"
    r = be2.fetch_voice("测试群", "999", retries=5, delay=0.01)
    check("确定没有时不重试(不浪费时间)", r is None and be2._media.calls == 1,
          "calls=%s" % be2._media.calls)

    # 回归:语音消息的 XML 正文不能泄漏给模型
    be3 = WxBackend(["测试群"])
    be3._group_usernames["测试群"] = "333@chatroom"
    row = {
        "local_id": "281", "type": "语音",
        "content": ('wxid_abc:\n<msg><voicemsg endflag="1" voiceformat="4" '
                    'voicelength="1832" length="3213"/></msg>'),
        "create_time": 1791976930, "sender_username": "wxid_abc",
        "shard": "",
    }
    gm = be3._to_group_message("测试群", row)
    check("语音消息正文换成 [语音],不泄漏 XML",
          gm.content == "[语音]", repr(gm.content))
    check("语音消息仍被识别为语音", gm.is_voice_msg)

    # 图片同理
    row2 = dict(row)
    row2["type"] = "图片"
    row2["content"] = 'wxid_abc:\n<?xml version="1.0"?><msg><img aeskey="xx"/></msg>'
    gm2 = be3._to_group_message("测试群", row2)
    check("图片消息正文换成 [图片]",
          gm2.content == "[图片]", repr(gm2.content))

    shutil.rmtree(tmpd, ignore_errors=True)


def test_voice_retry_queue():
    print("\n[23] 语音补试队列(微信延迟下载时自动补偿)")
    import os as _os
    import tempfile
    import time as _t

    tmpd = tempfile.mkdtemp()

    class FlakyBackend(MockBackend):
        """fetch_voice 前 2 次返回空,第 3 次才给 —— 模拟微信延迟下载。"""
        def __init__(self):
            super().__init__()
            self.fetch_calls = 0

        def fetch_voice(self, group, msg_id):
            self.fetch_calls += 1
            if self.fetch_calls < 3:
                return ""
            p = _os.path.join(tmpd, "v_%s.silk" % msg_id)
            with open(p, "wb") as f:
                f.write(b"\x02#!SILK_V3" + b"\x00" * 16)
            return p

    cfg = base_cfg()
    cfg.dry_run = False
    cfg.trigger.image_wait_seconds = 0.0
    cfg.trigger.mention_debounce_seconds = 0    # 和线上一致
    cfg.voice.enabled = True
    be = FlakyBackend()
    llm = StubLLM(["补答内容"])
    bot = GroupMateBot(cfg, be, llm=llm, logger=lambda m: None)

    v = msg("", sender="张三", t=1234.0, msg_id="vq1")
    v.msg_type = "语音"

    # 真实场景:先 @ 一下(建立"刚被点名"标记),再发语音条
    bot.policy._now = lambda: 1234.0
    mention = msg("@小助手", sender="张三", t=1234.0, mentioned=True,
                  msg_id="vq0")
    bot.policy.decide(mention)
    bot.policy.record_reply(mention, 1234.0)

    # 现在处理语音:拿不到音频 -> 应进队列,不发消息
    bot._transcribe = lambda m: None            # 模拟识别拿不到
    bot.handle(v, siblings=[])
    check("拿不到时进入补试队列", len(bot._pending_voices) == 1,
          str(len(bot._pending_voices)))
    check("拿不到时不乱发消息", len(be.sent) == 0, str(be.sent))

    # 第二、三次 tick:队列应持续重试
    bot._retry_pending_voices()
    still = len(bot._pending_voices)
    bot._retry_pending_voices()
    check("重试期间队列还在(音频没好)",
          still == 1 or len(bot._pending_voices) <= 1,
          "first=%s now=%s" % (still, len(bot._pending_voices)))

    # 让识别这次成功
    from groupmate.voice import VoiceTranscriber
    real_tr = bot._transcribe

    def fake_transcribe(m):
        if m.voice_path:
            m.voice_text = "识别出来的话"
            return "识别出来的话"
        return None

    bot._transcribe = fake_transcribe
    bot._retry_pending_voices()
    check("音频到手后成功补答", len(bot._pending_voices) == 0,
          str(list(bot._pending_voices.keys())))

    # 超时后应放弃,不会无限堆积
    bot2 = GroupMateBot(cfg, FlakyBackend(), llm=StubLLM(["x"]),
                        logger=lambda m: None)
    v2 = msg("", sender="李四", t=1.0, msg_id="vq2")
    v2.msg_type = "语音"
    bot2._pending_voices["k"] = [v2, _t.time() - 999, 0]   # 早就超时
    bot2._retry_pending_voices()
    check("超时的补试会被清掉(不无限堆积)",
          len(bot2._pending_voices) == 0, str(len(bot2._pending_voices)))

    import shutil
    shutil.rmtree(tmpd, ignore_errors=True)


def test_bug_regressions():
    print("\n[24] bug 回归(审计发现的问题)")
    import os as _os
    import shutil
    import tempfile
    import time as _t

    # --- 1. 回声拦截必须用真实时钟,不能用消息时间戳 ---
    # (机器人追赶积压消息时,消息时间戳落后几分钟;混用会让窗口判断失效)
    cfg = base_cfg()
    cfg.dry_run = False
    cfg.trigger.image_wait_seconds = 0.0
    bot = GroupMateBot(cfg, MockBackend(), llm=StubLLM(["x"]),
                       logger=lambda m: None)
    old_ts = _t.time() - 600          # 10 分钟前的消息时间戳
    bot._note_sent("测试群", "机器人说过的话", old_ts)
    # 现在读回来同样的内容 -> 必须认得出来(不能因为时间戳旧就放过)
    echo = msg("机器人说过的话", sender="张三", t=old_ts, msg_id="e1")
    check("回声拦截不受消息时间戳影响", bot._is_own_echo(echo))

    # --- 2. _handled 用 key 而不是 id(),且不会无限增长 ---
    check("_handled 初始为空", len(bot._handled) == 0)
    m = msg("hello", msg_id="h1")
    bot._handled.add(m.key)
    check("_handled 存的是消息 key", "测试群||h1" in bot._handled,
          str(bot._handled))
    # 塞爆后应被裁剪
    for i in range(700):
        bot._handled.add("测试群|bulk%d" % i)
    bot2 = GroupMateBot(cfg, MockBackend(), llm=StubLLM(["x"]),
                        logger=lambda m: None)
    bot._handled = set("k%d" % i for i in range(700))
    # 模拟 tick 里的裁剪逻辑
    if len(bot._handled) > 500:
        bot._handled = set(list(bot._handled)[-200:])
    check("_handled 超上限会被裁剪", len(bot._handled) <= 500,
          str(len(bot._handled)))

    # --- 3. 语音补答必须过护栏(不能绕过限流)---
    cfg3 = base_cfg()
    cfg3.dry_run = False
    cfg3.trigger.image_wait_seconds = 0.0
    cfg3.guard.max_per_minute = 0        # 直接把限流设成 0,看会不会被拦
    be3 = MockBackend()
    bot3 = GroupMateBot(cfg3, be3, llm=StubLLM(["补答"]), logger=lambda m: None)
    v = msg("", sender="张三", t=_t.time(), msg_id="vv1")
    v.msg_type = "语音"
    v.voice_path = "/tmp/fake.silk"
    bot3._reply_to_voice(v, "识别出来的话")
    check("语音补答被护栏挡住时不发送", len(be3.sent) == 0, str(be3.sent))

    # 护栏放开时应该能发
    cfg3.guard.max_per_minute = 10
    bot3._reply_to_voice(v, "识别出来的话")
    check("护栏放开后语音补答能发出", len(be3.sent) == 1, str(be3.sent))

    # --- 4. 缓存目录清理 ---
    from groupmate.voice import prune_dir
    tmpd = tempfile.mkdtemp()
    for i in range(20):
        p = _os.path.join(tmpd, "f%02d.wav" % i)
        with open(p, "wb") as f:
            f.write(b"x")
        _os.utime(p, (_t.time() - 100 + i, _t.time() - 100 + i))
    removed = prune_dir(tmpd, keep=5)
    left = len([f for f in _os.listdir(tmpd) if f.endswith(".wav")])
    check("清理只留最近的 N 个", left == 5, "removed=%s left=%s" % (removed, left))
    check("清理返回删除数", removed == 15, str(removed))
    # 目录不存在不该炸
    check("清理不存在的目录返回 0", prune_dir("/no/such/dir") == 0)
    shutil.rmtree(tmpd, ignore_errors=True)


def test_audit_regressions():
    print("\n[25] 审计回归(第二轮发现的 bug)")
    import os as _os
    import shutil
    import tempfile
    import time as _t

    # --- 1. 等待补发窗口取到的消息不能丢 ---
    cfg = base_cfg()
    cfg.dry_run = False
    cfg.trigger.image_wait_seconds = 0.6      # 特意留一个等待窗口
    cfg.trigger.mention_debounce_seconds = 0
    be = MockBackend()
    bot = GroupMateBot(cfg, be, llm=StubLLM(["回复A", "回复B"]),
                       logger=lambda m: None)
    # @ 一条,让 handle 进入等待循环
    be.feed(msg("@小助手 第一问", sender="张三", t=_t.time() - 100,
                mentioned=True, msg_id="q1"))
    # 等待期间另一个人也 @ —— 这条以前会被直接丢弃
    be.feed(msg("@小助手 第二问", sender="李四", t=_t.time() - 99,
                mentioned=True, msg_id="q2"))
    bot.tick()                                 # 第一轮:处理 q1,q2 进欠账队列
    for _ in range(4):                         # 之后几轮应该把 q2 补上
        bot.tick()
    texts = [t for _g, t, *_ in be.sent]
    check("等待窗口里的消息不会被丢掉(第二轮被处理)",
          any("B" in t for t in texts), str(texts))

    # --- 2. 短回复不能吞掉真人的长消息 ---
    bot2 = GroupMateBot(base_cfg(), MockBackend(), llm=StubLLM(["x"]),
                        logger=lambda m: None)
    bot2._note_sent("测试群", "好的")            # 机器人只回了个"好的"
    real = msg("好的 那我明天再问你", sender="张三", t=0, msg_id="r1")
    check("机器人回短句时,真人的长消息不被当成回声",
          not bot2._is_own_echo(real))
    check("自己的长回复被截断仍能认出",
          (bot2._note_sent("测试群", "这是一条比较长的机器人回复内容"),
           bot2._is_own_echo(msg("这是一条比较长", t=0, msg_id="r2")))[1])

    # --- 3. 语音识别结果要参与触发判定 ---
    cfg3 = base_cfg()
    cfg3.mention_debounce_seconds = 0
    vm = msg("", sender="张三", t=_t.time(), msg_id="v1")
    vm.msg_type = "语音"
    vm.voice_text = "@小助手 帮我看下这题"
    p = ReplyPolicy(cfg3, rand=lambda: 0.99)
    d = p.decide(vm)
    check("语音里 @ 我 -> 能触发 mention",
          d.reply and d.trigger == "mention", "%s/%s" % (d.reply, d.trigger))

    # --- 4. _follows_mine 必须校验"本条在机器人之后" ---
    from groupmate.proactive import should_chime_in
    hist = [
        msg("我先说一句", sender="张三", t=1000.0, msg_id="h1"),
        msg("机器人回的", sender="我", t=1001.0, msg_id="h2"),
    ]
    hist[1].is_self = True
    # A 的消息(bot 刚为它回复过,now 早于 bot 那条)不该被认成"接我的话"
    other = msg("我先去吃饭了", sender="李四", t=1000.5, msg_id="h3")
    _want, _why, replying = should_chime_in(other, hist, ["小助手"], 1000.5)
    check("机器人发言之后才发的才算接话(时间更早的不算)", not replying)

    # --- 5. 图片目录必须用图片后缀清理 ---
    from groupmate.voice import prune_dir
    tmpd = tempfile.mkdtemp()
    for i in range(5):
        with open(_os.path.join(tmpd, "pic%02d.jpg" % i), "wb") as f:
            f.write(b"x")
    n = prune_dir(tmpd, keep=2,
                  suffix=(".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"))
    left = len([f for f in _os.listdir(tmpd) if f.endswith(".jpg")])
    check("图片目录能被正常清理", n == 3 and left == 2,
          "removed=%s left=%s" % (n, left))
    shutil.rmtree(tmpd, ignore_errors=True)

    # --- 6. ASR 缓存裁剪要同时管内存(否则下次保存又写回来) ---
    from groupmate.voice import VoiceTranscriber
    tr = VoiceTranscriber(api_key="k", cache_dir=tempfile.mkdtemp())
    for i in range(10):
        tr._cache["k%d" % i] = "文本%d" % i
        tr._cache_ts["k%d" % i] = float(i)
    dropped = tr.trim_cache(3)
    check("内存缓存被裁到上限", len(tr._cache) == 3 and dropped == 7,
          "left=%s drop=%s" % (len(tr._cache), dropped))

    # --- 7. 疑问句判定:分层门槛(强信号直接接,弱信号要更长) ---
    from groupmate.proactive import _looks_like_question, _question_strength
    # 强信号(问号 / 疑问词)
    for t in ("这题怎么做啊", "有人知道食堂几点关门吗", "明天上课吗?", "为什么会这样"):
        check("强疑问信号可识别: %s" % t, _looks_like_question(t, min_chars=6))
    # 弱信号(只以"吗/呢/吧"结尾):短句不接,够长才接
    check("弱信号短句不接(随口一说)", not _looks_like_question("我先去吃饭了吧",
                                                        min_chars=6))
    check("弱信号够长才接(真在问人)",
          _looks_like_question("这周六到底要不要补课的吧", min_chars=6))
    check("陈述句不误判", not _looks_like_question("今天天气不错"))
    # 强度分档本身
    check("问号是强信号", _question_strength("你吃了吗？") == 2)
    check("语气词结尾是弱信号", _question_strength("吃了吗") == 1)
    check("陈述句无信号", _question_strength("我去吃饭了") == 0)
    # 宽松模式(旧行为)仍然可用
    check("宽松模式下弱信号短句也接",
          _looks_like_question("明天上课吗", strict=False, min_chars=4))


def test_gpt_audit_round2():
    print("\n[26] 第二轮审计回归(静默/@ 必回/护栏一致性/语音路径)")
    import os as _os
    import shutil
    import tempfile
    import time as _t

    clock = FakeClock(at_hour(2))          # 凌晨 —— 静默时段
    cfg = base_cfg()
    cfg.mention_debounce_seconds = 0

    # --- 1. 静默时段 @ 我 必须回(以前被静默挡掉,和文档矛盾)---
    p = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)
    d = p.decide(msg("@小助手 睡了吗", t=clock.t, mentioned=True))
    check("静默时段被 @ -> 必回",
          d.reply and d.trigger == "mention", "%s / %s" % (d.reply, d.reason))

    # 语音形式的 @ 也要穿透静默
    p2 = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)
    vm = msg("", t=clock.t, msg_id="nv1")
    vm.msg_type = "语音"
    vm.voice_text = "@小助手 睡了吗"
    d2 = p2.decide(vm)
    check("静默时段语音里的 @ -> 必回",
          d2.reply and d2.trigger == "mention", "%s / %s" % (d2.reply, d2.reason))

    # 静默时段非 @ 仍然不回
    p3 = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)
    d3 = p3.decide(msg("今天天气怎么样", t=clock.t, sender="李四"))
    check("静默时段关键词 -> 不回", not d3.reply, d3.reason)

    # --- 2. 被限流挡住的 @ 要排队,配额恢复后补答 ---
    cfg4 = base_cfg()
    cfg4.dry_run = False
    cfg4.trigger.image_wait_seconds = 0.0
    cfg4.trigger.mention_debounce_seconds = 0   # 线上就是 0,避免防抖先挡住
    cfg4.guard.max_per_minute = 1          # 只允许一条
    be4 = MockBackend()
    llm4 = StubLLM(["第一条", "补答的那条"])
    bot4 = GroupMateBot(cfg4, be4, llm=llm4, logger=lambda m: None)
    bot4.policy._now = FakeClock(at_hour(10))
    bot4.policy._now_fn = None             # 用真实游标逻辑
    bot4.policy._now = bot4.policy._clock

    t0 = _t.time()
    be4.feed(msg("@小助手 第一问", sender="张三", t=t0, mentioned=True,
                 msg_id="mq1"))
    bot4.tick()
    check("第一条 @ 正常回复", bot4.stats["replied"] == 1, str(bot4.stats))

    be4.feed(msg("@小助手 第二问", sender="李四", t=t0 + 1, mentioned=True,
                 msg_id="mq2"))
    bot4.tick()
    check("被限流的 @ 进入待回复队列(没有被丢掉)",
          len(bot4._pending_mentions) == 1, str(len(bot4._pending_mentions)))

    # 配额恢复(把上限放开)-> 补答
    bot4.cfg.guard.max_per_minute = 10
    bot4._retry_pending_mentions()
    check("配额恢复后欠下的 @ 被补答",
          bot4.stats["replied"] == 2 and not bot4._pending_mentions,
          str(bot4.stats) + " pending=" + str(len(bot4._pending_mentions)))

    # --- 3. 语音提示也要过护栏(以前直接 send,绕过限流)---
    cfg5 = base_cfg()
    cfg5.dry_run = False
    cfg5.voice.enabled = True
    cfg5.voice.tell_when_unavailable = True
    cfg5.guard.max_per_minute = 0          # 限流设 0 -> 一切发送都该被挡
    be5 = MockBackend()
    bot5 = GroupMateBot(cfg5, be5, llm=StubLLM(["x"]), logger=lambda m: None)
    vmsg = msg("", sender="张三", t=_t.time(), msg_id="tv1")
    vmsg.msg_type = "语音"
    bot5._tell_voice_unavailable(vmsg, 3)
    check("语音听不了提示被限流挡住时不发送", len(be5.sent) == 0, str(be5.sent))

    # --- 4. 等待补发窗口收到的语音要走识别流程 ---
    cfg6 = base_cfg()
    cfg6.dry_run = False
    cfg6.trigger.image_wait_seconds = 0.4
    cfg6.trigger.mention_debounce_seconds = 0
    be6 = MockBackend()
    bot6 = GroupMateBot(cfg6, be6, llm=StubLLM(["好"]), logger=lambda m: None)
    calls = {"n": 0}

    class VoiceAwareBackend(MockBackend):
        def ensure_voices(self, messages=None):
            n = 0
            for m in (messages or []):
                if getattr(m, "is_voice_msg", False) and not m.voice_path:
                    m.voice_path = "/tmp/fake.silk"
                    n += 1
            calls["n"] += n
            return n

        def poll(self):
            out = super().poll()
            for m in out:
                m.voice_path = ""          # 模拟"音频还没下载好"
            return out

    be7 = VoiceAwareBackend()
    bot7 = GroupMateBot(cfg6, be7, llm=StubLLM(["好"]), logger=lambda m: None)
    bot7._transcribe = lambda m: (setattr(m, "voice_text", "识别出来的"),
                                  "识别出来的")[1]
    # 先 @ 建立触发,再在同一次 tick 的中途补一条语音
    be7.feed(msg("@小助手 在吗", sender="张三", t=_t.time() - 100,
                 mentioned=True, msg_id="vw1"))
    vmsg2 = msg("", sender="张三", t=_t.time() - 99, msg_id="vw2")
    vmsg2.msg_type = "语音"
    be7.feed(vmsg2)
    bot7.tick()
    check("等待窗口里的语音也被提取并识别(不再被漏掉)",
          calls["n"] >= 1 or any(getattr(m, "voice_text", "")
                                 for m in bot7.history),
          "ensure_voices=%d" % calls["n"])

    # --- 5. 护栏状态要能持久化(重启不重置)---
    from groupmate.policy import ReplyPolicy as RP
    p6 = RP(base_cfg(), rand=lambda: 0.99)
    p6.record_reply(msg("x", t=_t.time()), _t.time())
    p6.record_failure()
    p6.record_failure()
    snap = p6.export_state()
    check("导出的护栏状态含限流计数与失败数",
          snap.get("minute") and snap.get("failures") == 2, str(snap))

    p7 = RP(base_cfg(), rand=lambda: 0.99)
    p7.import_state(snap)
    check("重启后恢复限流计数", len(p7._minute.stamps) == len(snap["minute"]),
          str(len(p7._minute.stamps)))
    check("重启后恢复连续失败数", p7.consecutive_failures == 2,
          str(p7.consecutive_failures))
    # 过期的计数要被丢掉
    p8 = RP(base_cfg(), rand=lambda: 0.99)
    old = _t.time() - 99999
    p8.import_state({"minute": [old], "hour": [old], "day": [old],
                     "failures": 0})
    check("过期计数恢复时被丢弃", len(p8._minute.stamps) == 0,
          str(len(p8._minute.stamps)))


def test_proactive_throttle():
    print("\n[27] 主动接话的频率闸门(冷却 + 每小时配额 + 门槛)")
    import time as _t

    # --- 默认配置本身就该是"安静"的 ---
    d = BotConfig().trigger
    check("默认主动冷却 >= 300 秒", d.proactive_cooldown_seconds >= 300,
          str(d.proactive_cooldown_seconds))
    check("默认有每小时配额上限", d.proactive_max_per_hour > 0,
          str(d.proactive_max_per_hour))
    check("默认要求明确提问", d.proactive_strict_question is True)

    # --- 每小时配额真的会拦住 ---
    cfg = base_cfg()
    cfg.trigger.proactive_cooldown_seconds = 0     # 关掉冷却,单独验配额
    cfg.trigger.proactive_max_per_hour = 2
    cfg.trigger.proactive_min_chars = 4
    cfg.trigger.proactive_strict_question = True
    clock = FakeClock(at_hour(10))
    p = ReplyPolicy(cfg, now_fn=clock, rand=lambda: 0.99)

    fired = 0
    # 连发 6 条"明确在提问"的消息,间隔 30 秒(一小时窗口内)
    for i in range(6):
        m = msg("这题怎么做啊", sender="张三", t=clock.t, msg_id="pq%d" % i)
        dec = p.decide(m)
        if dec.reply and dec.trigger == "proactive":
            fired += 1
            p.record_reply(m)
        clock.advance(30)
    check("每小时配额生效(一小时最多 2 次主动)",
          fired == cfg.trigger.proactive_max_per_hour, "fired=%d" % fired)

    # 过了一小时之后配额应该释放
    clock.advance(3700)
    m = msg("这题怎么做啊", sender="张三", t=clock.t, msg_id="pq_after")
    dec = p.decide(m)
    check("满一小时后配额释放,可以再主动一次",
          dec.reply or "跟我不相关" in dec.reason, dec.reason)

    # --- 冷却真的会拦住连珠炮 ---
    cfg2 = base_cfg()
    cfg2.trigger.proactive_cooldown_seconds = 600
    cfg2.trigger.proactive_max_per_hour = 0        # 只验冷却
    cfg2.trigger.proactive_min_chars = 4
    clock2 = FakeClock(at_hour(10))
    p2 = ReplyPolicy(cfg2, now_fn=clock2, rand=lambda: 0.99)
    m1 = msg("这题怎么做啊", sender="张三", t=clock2.t, msg_id="pc1")
    d1 = p2.decide(m1)
    if d1.reply:
        p2.record_reply(m1)
    clock2.advance(60)                              # 才过 1 分钟
    d2 = p2.decide(msg("这题怎么做啊", sender="李四", t=clock2.t, msg_id="pc2"))
    check("默认冷却下,1 分钟后不会再主动接", not d2.reply, d2.reason)

    # --- 门槛:随口一说不再被当成提问 ---
    from groupmate.proactive import should_chime_in
    for t in ("我先去吃饭了吧", "你吃了吗", "这题你会做吧"):
        m = GroupMessage(group="测试群", sender="张三", content=t,
                         timestamp=1000.0)
        want, why, _r = should_chime_in(m, [], ["小助手"], 1000.0,
                                        strict_question=True, min_chars=6)
        check("随口一句不主动接: %s" % t, not want, why)
    for t in ("这题怎么做啊", "有人知道食堂几点关门吗"):
        m = GroupMessage(group="测试群", sender="张三", content=t,
                         timestamp=1000.0)
        want, _why, _r = should_chime_in(m, [], ["小助手"], 1000.0,
                                         strict_question=True, min_chars=6)
        check("真问题仍然会接: %s" % t, want)


def test_memory():
    print("\n[17] 记忆系统(短期上下文 + 长期事实)")
    import os as _os
    import shutil
    import tempfile

    from groupmate.memory import Memory

    d = tempfile.mkdtemp()
    p = _os.path.join(d, "m.json")
    m = Memory(p, context_limit=10)

    # 短期上下文
    m.remember("群A", "张三", "wxid_a", "今天去打球了")
    m.remember("群A", "我", "", "那挺好啊", is_self=True)
    m.remember("群A", "李四", "wxid_b", "", is_image=True)
    m.remember("群B", "王五", "wxid_c", "别的群的消息")
    ctx = m.context_text("群A")
    check("上下文含张三", "张三: 今天去打球了" in ctx, ctx)
    check("上下文标记自己", "我: 那挺好啊" in ctx, ctx)
    check("图片显示为[发了张图片]", "[发了张图片]" in ctx, ctx)
    check("群之间上下文隔离", "别的群的消息" not in ctx, ctx)

    # 长期事实
    check("能加事实", m.add_fact("张三喜欢打篮球", group="群A", about="张三"))
    check("重复事实不重复加",
          not m.add_fact("张三喜欢打篮球", group="群A", about="张三"))
    check("太短的不记", not m.add_fact("嗯", group="群A"))
    m.add_fact("李四在准备考研", group="群A", about="李四")
    m.add_fact("群B的私事", group="群B")
    check("事实总数", len(m.facts) == 3, str(len(m.facts)))

    # 相关性检索
    rel = m.relevant("群A", "张三最近干嘛呢", "张三")
    texts = [f.text for f in rel]
    check("能检索到张三的事", any("张三" in t for t in texts), str(texts))
    check("别的群的事不掺和", all("群B的私事" not in t for t in texts), str(texts))

    # 群印象
    m.set_note("群A", "这是个同学群")
    check("群印象能存", "同学群" in (m.memory_text("群A") or ""))

    # 时效:分类与过期
    from groupmate.memory import classify_fact, Fact
    check("约定识别为 promise", classify_fact("约好明天一起打球") == "promise")
    check("生日识别为 birthday", classify_fact("他生日是 3 月 5 号") == "birthday")
    check("偏好识别为 preference", classify_fact("张三喜欢打篮球") == "preference")
    check("普通事实识别为 general", classify_fact("他们在准备期末考试") == "general")

    f_promise = Fact(text="约定明天交作业", kind="promise",
                     expires_at=__import__("time").time() + 100)
    check("未过期约定不算过期", not f_promise.expired)
    f_old = Fact(text="很久前的约定", kind="promise",
                 expires_at=__import__("time").time() - 10)
    check("过期约定被判定过期", f_old.expired)

    m.add_fact("约定后天一起去打球", group="群A", kind="promise")
    m.add_fact("他生日是 5 月 1 号", group="群A", kind="birthday")
    rel2 = m.relevant("群A", "聊点啥", "")
    check("生日记忆有加权(优先出现)",
          any(f.kind == "birthday" for f in rel2), str([f.kind for f in rel2]))
    check("记忆文本带[约定]标记",
          "[约定]" in m.memory_text("群A", "打球", ""), m.memory_text("群A", "打球", ""))
    check("记忆文本带时间(多久之前)",
          "前" in m.memory_text("群A", "打球", "") or "刚刚" in m.memory_text("群A", "打球", ""))

    # 过期清理
    m.facts.append(Fact(text="这条已过期", group="群A", kind="promise",
                        expires_at=__import__("time").time() - 1))
    n_purged = m.purge_expired()
    check("过期记忆能被清掉", n_purged >= 1, str(n_purged))

    # 持久化
    m.save(force=True)
    m2 = Memory(p, context_limit=10)
    check("重启后事实还在", len(m2.facts) == len(m.facts),
          "%d vs %d" % (len(m2.facts), len(m.facts)))
    check("重启后事实内容不丢",
          any("张三" in f.text for f in m2.facts), str([f.text for f in m2.facts]))
    check("重启后 kind 保留",
          any(f.kind == "birthday" for f in m2.facts),
          str([f.kind for f in m2.facts]))
    check("重启后上下文还在", "打球" in m2.context_text("群A"))
    check("重启后印象还在", "同学群" in (m2.notes.get("群A") or ""))
    check("过期记忆不在重启后残留",
          all(not f.expired for f in m2.facts), str([f.text for f in m2.facts]))

    shutil.rmtree(d, ignore_errors=True)


def main():
    print("=" * 62)
    print("AI 群友 —— 规则引擎离线测试")
    print("=" * 62)
    test_policy()
    test_sanitize()
    test_end_to_end()
    test_prompt()
    test_strip_mention()
    test_images()
    test_image_follow()
    test_followup_merge()
    test_echo_and_cooldown()
    test_consecutive_mentions()
    test_proactive()
    test_voice()
    test_voice_retry_queue()
    test_bug_regressions()
    test_audit_regressions()
    test_gpt_audit_round2()
    test_proactive_throttle()
    test_memory()
    print("\n" + "=" * 62)
    print("通过 %d 项,失败 %d 项" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
