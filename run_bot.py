# -*- coding: utf-8 -*-
"""启动入口。

常用命令
--------
  python run_bot.py check          # 只做体检:环境/微信/数据库/群名/API Key
  python run_bot.py demo           # 离线演示:不连微信,用假消息跑一遍规则
  python run_bot.py run            # 真跑(受 config.json 的 dry_run 控制)

首次请务必先 check,再 demo,最后把 config.json 里 dry_run 改成 false 才真发。
"""
from __future__ import annotations

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from groupmate.config import BotConfig


def cmd_check() -> int:
    print("=" * 64)
    print("AI 群友 —— 环境体检")
    print("=" * 64)
    cfg = BotConfig.load(os.path.join(HERE, "config.json"))
    problems = []

    print("\n[1] Python 与依赖")
    print("    Python :", sys.version.split()[0],
          "(%d 位)" % (8 * __import__("struct").calcsize("P")))
    for mod, hint in [("wechatauto", "pip install wechatauto-replica"),
                      ("PIL", "pip install pillow"),
                      ("comtypes", "pip install comtypes"),
                      ("uiautomation", "pip install uiautomation")]:
        try:
            __import__(mod)
            print("    %-14s ✓" % mod)
        except Exception as e:
            print("    %-14s ✗  (%s)" % (mod, hint))
            problems.append("缺依赖 %s: %s" % (mod, hint))

    print("\n[2] 微信进程")
    from groupmate.wx_backend import detect_weixin_pids
    pids = detect_weixin_pids()
    if pids:
        print("    找到 Weixin.exe PID:", pids)
    else:
        print("    ✗ 没找到 Weixin.exe —— 微信没登录?")
        problems.append("未检测到微信进程")

    print("\n[3] 数据目录")
    try:
        from wechatauto import auto_detect_db_dir, list_accounts
        d = auto_detect_db_dir()
        print("    数据目录:", d)
        for a in list_accounts():
            print("    账号:", a.get("account"), "|", a.get("path"))
    except Exception as e:
        print("    ✗ 定位失败:", e)
        problems.append("定位微信数据目录失败")

    print("\n[4] 数据库密钥(决定能否读消息)")
    try:
        from wechatauto import WeChatDB
        if pids and not os.environ.get("WECHATAUTO_PID"):
            os.environ["WECHATAUTO_PID"] = ",".join(str(p) for p in pids)
        db = WeChatDB()
        keys = db.extract_keys()
        if keys:
            print("    ✓ 提取到 %d 个库密钥 —— 可以读消息" % len(keys))
        else:
            print("    ✗ 提取到 0 个密钥 —— 读不了消息(发送仍可能可用)")
            print("      常见原因:脚本权限/完整性级别与微信不一致;")
            print("      或该微信版本的内存布局未收录,把版本号反馈给库作者。")
            problems.append("数据库密钥提取失败(只能发,不能读)")
    except Exception as e:
        print("    ✗ 失败:", e)
        problems.append("数据库不可用: %s" % e)

    print("\n[5] 配置检查")
    print("    干跑模式 :", cfg.dry_run, "(true=不会真发)")
    if cfg.dry_run:
        print("      ↑ 记得改 config.json 的 dry_run 为 false 才会真的回复")
    if not cfg.trigger.groups or cfg.trigger.groups == ["把这里改成你的群名"]:
        print("    ✗ 还没设置群名(trigger.groups)")
        problems.append("未配置目标群")
    else:
        print("    目标群   :", cfg.trigger.groups)
    print("    触发方式 : @%s 或关键词 %s" % (cfg.trigger.mention_names,
                                              cfg.trigger.keywords or "(无)"))
    print("    限流     : %d/分钟 %d/小时 %d/天"
          % (cfg.guard.max_per_minute, cfg.guard.max_per_hour, cfg.guard.max_per_day))
    print("    深夜静默 :", cfg.trigger.quiet_hours)

    print("\n[6] 大模型")
    from groupmate.llm import ChatLLM
    llm = ChatLLM(cfg.llm)
    if llm.ready:
        print("    ✓ 已检测到 API Key(来自环境变量 %s)" % cfg.llm.api_key_env)
    else:
        print("    ✗ 没有 API Key,请设置环境变量 %s" % cfg.llm.api_key_env)
        problems.append("缺少 %s" % cfg.llm.api_key_env)

    print("\n" + "=" * 64)
    if problems:
        print("发现 %d 个问题:" % len(problems))
        for p in problems:
            print("  -", p)
    else:
        print("全部通过 ✓")
    print("=" * 64)
    return 1 if problems else 0


def cmd_demo() -> int:
    """离线演示:用脚本化消息跑一遍决策流程,不需要微信和 API Key。"""
    from groupmate.backend import MockBackend
    from groupmate.bot import GroupMateBot
    from groupmate.message import GroupMessage

    print("=" * 64)
    print("离线演示(不连微信,不会发任何消息)")
    print("=" * 64)
    cfg = BotConfig.load(os.path.join(HERE, "config.json"))
    cfg.trigger.groups = ["测试群"]
    cfg.trigger.mention_names = ["小助手"]
    cfg.trigger.keywords = ["天气"]
    cfg.trigger.quiet_hours = []
    cfg.trigger.image_wait_seconds = 0      # 演示不等"补发内容",否则要等好几秒
    cfg.trigger.mention_debounce_seconds = 0
    cfg.trigger.per_user_cooldown_seconds = 2
    cfg.trigger.per_group_cooldown_seconds = 2
    cfg.trigger.self_silence_seconds = 1
    cfg.trigger.proactive_cooldown_seconds = 3
    cfg.dry_run = True
    # 演示不要污染项目目录:记忆写到临时文件里
    demp_mem = os.path.join(__import__("tempfile").gettempdir(),
                            "groupmate_demo_memory.json")
    try:
        if os.path.exists(demp_mem):
            os.remove(demp_mem)
    except OSError:
        pass

    now = __import__("time").time()
    # 每条之间留足间隔,免得被"补发内容合并"逻辑合到一起,看不清单条行为
    script = [
        # ① 没 @、没关键词、也不像问问题 -> 应该不插嘴
        GroupMessage(group="测试群", sender="张三", content="我先去吃饭了",
                     timestamp=now),
        # ② 没 @,但像在问大家 -> 主动接一句
        GroupMessage(group="测试群", sender="李四", content="有人知道食堂几点关门吗",
                     timestamp=now + 300),
        # ③ 关键词触发
        GroupMessage(group="测试群", sender="王五", content="今天天气怎么样",
                     timestamp=now + 600),
        # ④ @ 必回
        GroupMessage(group="测试群", sender="孙七", content="@小助手 你在吗",
                     timestamp=now + 900, mentioned_me=True),
        # ⑤ 别的群的消息 -> 白名单挡掉
        GroupMessage(group="别的群", sender="赵六", content="@小助手 你好",
                     timestamp=now + 1200, mentioned_me=True),
    ]

    class EchoLLM:
        """演示用假模型:证明「决策通过 -> 会调用模型 -> 内容会发出去」这条链路。

        ⚠ 签名必须和 llm.ChatLLM.reply 一致(images / mention_names),
        否则接到真实调用时会 TypeError —— 这是新人第一次改代码最容易踩的坑。
        """
        ready = True

        def reply(self, msg, history, system_prompt, images=None,
                  mention_names=None):
            extra = ""
            if images:
                extra = "(还带了 %d 张图)" % len(images)
            return "我是演示回复：你说的是「%s」对吧%s" % (
                (msg.content or "")[:20], extra)

    backend = MockBackend(script)
    bot = GroupMateBot(cfg, backend, llm=EchoLLM())
    bot.memory.path = demp_mem            # 演示用临时记忆文件
    bot.run(poll_interval=0, max_loops=1)

    print("\n" + "=" * 64)
    print("会发出的消息(演示是干跑模式,没有真的发):")
    for grp, text, tag in bot.outbox:
        print("  [%s](%s) %s" % (grp, tag, text))
    print("\n说明(对照上面的日志看):")
    print("  ① 「我先去吃饭了」  没 @、没关键词、不像问问题 -> 不插嘴")
    print("  ② 「有人知道食堂几点关门吗」 像在问大家 -> 主动接一句")
    print("  ③ 「今天天气怎么样」 命中关键词 -> 回")
    print("  ④ 「@小助手 你在吗」 被 @ -> 必回")
    print("  ⑤ 「别的群」那条  不在群白名单 -> 读都不读,直接挡掉")
    print("  每条回复都过了冷却和限流,不会刷屏;当前是干跑模式,没有真的发。")
    print("\n统计:", bot.stats)
    print("=" * 64)
    return 0


def _need_config() -> bool:
    """config.json 不存在时给出清晰指引(第一次用最容易卡在这)。"""
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        return True
    print("=" * 64)
    print("还没创建 config.json")
    print("=" * 64)
    print("先复制一份模板,再改里面的群名:")
    print()
    print("    copy config.example.json config.json")
    print()
    print("然后至少改这两处:")
    print('    "groups":        ["你的群名"]      <- 必须和微信里显示的一字不差')
    print('    "mention_names": ["小助手"]        <- 群里 @ 这个名字就触发')
    print()
    print("改完先跑体检:  python run_bot.py check")
    print("=" * 64)
    return False


def cmd_run() -> int:
    from groupmate.bot import GroupMateBot
    from groupmate.wx_backend import WxBackend

    if not _need_config():
        return 2

    cfg = BotConfig.load(os.path.join(HERE, "config.json"))
    if not cfg.trigger.groups or cfg.trigger.groups == ["把这里改成你的群名"]:
        print("=" * 64)
        print("还没设置群名")
        print("=" * 64)
        print('请编辑 config.json,把 trigger.groups 改成真实的群名:')
        print('    "groups": ["你的群名"]')
        print()
        print("不确定群名?先随便填一个再启动,日志里会打印本机的群名列表")
        print('(找 "本机群名: ..." 那一行),复制准确的过来即可。')
        print("=" * 64)
        return 2
    if cfg.dry_run:
        print(">>> 当前是【干跑模式】:只会打印想发的内容,不会真的发到群里。")
        print(">>> 确认规则无误后,把 config.json 里的 dry_run 改成 false。")

    backend = WxBackend(cfg.trigger.groups, poll_interval=2.0,
                        account=cfg.wechat_account,
                        mention_names=cfg.trigger.mention_names)
    bot = GroupMateBot(cfg, backend)
    stats = bot.run(poll_interval=1.0)
    print("统计:", stats)
    return 0


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
    if cmd in ("check", "体检", "doctor"):
        return cmd_check()
    if cmd in ("demo", "演示"):
        return cmd_demo()
    if cmd in ("run", "运行", "start"):
        return cmd_run()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
