# 安装指南(逐步操作)

> 这份文档假设你**从零开始**,机器上什么都没装。
> 全程大约 30 分钟,按顺序做即可。
>
> 如果你是一个 AI Agent 在帮人部署:每一步后面都写了**预期输出**,
> 对不上就停下看 [README 第 7 节](README.md#7-排错)。

---

## 目录

- [第 0 步:先做决策](#第-0-步先做决策)
- [第 1 步:装 Python](#第-1-步装-python)
- [第 2 步:装微信](#第-2-步装微信)
- [第 3 步:(推荐)装虚拟机](#第-3-步推荐装虚拟机)
- [第 4 步:下载本项目](#第-4-步下载本项目)
- [第 5 步:装依赖](#第-5-步装依赖)
- [第 6 步:拿 API Key](#第-6-步拿-api-key)
- [第 7 步:写配置](#第-7-步写配置)
- [第 8 步:体检](#第-8-步体检)
- [第 9 步:离线演示](#第-9-步离线演示)
- [第 10 步:干跑](#第-10-步干跑)
- [第 11 步:正式运行](#第-11-步正式运行)
- [第 12 步:开机自启(可选)](#第-12-步开机自启可选)
- [验收清单](#验收清单)

---

## 第 0 步:先做决策

**在动手之前先想清楚这三件事**,否则装到一半会返工:

| 决策 | 选项 | 建议 |
|---|---|---|
| **用哪个微信号** | 主号 / 小号 | **强烈建议小号**。UI 自动化有风控风险 |
| **在哪跑** | 本机 / 虚拟机 | **强烈建议虚拟机**。隔离风险 |
| **管哪些群** | 单群 / 多群 | 先用**一个群**验证,跑通了再扩 |

> ⚠️ **本软件会把群消息发给你配置的大模型 API(如 DeepSeek)。**
> 不是本地推理。介意的话就别用,或者换成群里没有敏感信息的测试群。

---

## 第 1 步:装 Python

1. 打开 https://www.python.org/downloads/windows/
2. 下载 **Python 3.12**(或 3.10 / 3.11,都行;**不要 3.9 以下**)
3. 安装时 **务必勾选 `Add python.exe to PATH`** ← 最容易漏的一步
4. 装完打开 **PowerShell**,验证:

```powershell
python --version
```

**预期输出**:`Python 3.12.x`

如果提示"无法将 python 识别为 cmdlet",说明没勾 PATH,重装一遍并勾上。

---

## 第 2 步:装微信

1. 去 https://pc.weixin.qq.com/ 下载 **Windows 版**
2. 安装并**登录你的小号**
3. **重要**:登录后**不要关闭微信窗口**,也不要让它退出登录

验证:微信窗口能看到,左侧有会话列表。

> 📌 **固定微信版本,关掉自动更新。**
> 本项目靠读取微信本地数据库工作,微信大版本更新可能改库结构。

---

## 第 3 步:(推荐)装虚拟机

**跳过这步也能用,但风险自己承担。** 建议流程:

1. 下载 **VirtualBox**(免费):https://www.virtualbox.org/
2. 新建虚拟机:
   - 类型:Microsoft Windows / Windows 10 (64-bit)
   - 内存:**4096 MB**
   - CPU:**2 核**
   - 磁盘:**50 GB**
   - **不需要显卡加速**
3. 装 Windows 10(需要 ISO 镜像)
4. 虚拟机里重复**第 1、2 步**(装 Python + 微信)
5. 从这里开始,**所有操作都在虚拟机里做**

**为什么要虚拟机**:万一把小号搞封了,影响的只是虚拟机里的那个号;而且虚拟机可以随时快照回滚。

> 💡 虚拟机里跑不需要 GPU,4 GB 内存足够。宿主机有 8 GB 以上就够用。

---

## 第 4 步:下载本项目

```powershell
# 用 git(如果装了)
git clone <仓库地址>
cd wechat-ai-groupmate

# 或者:直接在 GitHub 页面点 Code → Download ZIP,解压后进去
```

---

## 第 5 步:装依赖

```powershell
# 建虚拟环境(推荐,避免污染系统 Python)
python -m venv .venv

# 激活
.venv\Scripts\activate

# 装依赖
pip install -r requirements.txt
```

**预期输出**:最后几行是 `Successfully installed ...`

**验证**(逐条跑,都不该报错):

```powershell
python -c "import wechatauto; print('微信通道 OK')"
python -c "import uiautomation; print('UI 自动化 OK')"
python -c "import PIL; print('图片处理 OK')"
python -c "from pysilk import decode; print('语音解码 OK')"
```

> **如果 `pysilk` 装不上**:确认用的是 `silk-python`(有预编译包)而不是 `pilk`
> (需要 C 编译器)。`pip install silk-python` 即可。

---

## 第 6 步:拿 API Key

### 6.1 DeepSeek Key(必需)

1. 打开 https://platform.deepseek.com
2. 注册 / 登录 → 左侧 **API keys** → **创建**
3. 复制 Key(`sk-` 开头)
4. **充值一点点**(几块钱够测很久)

### 6.2 语音识别 Key(可选)

**不配也能跑**,只是它听不了语音(而且会如实告诉群里)。

1. 打开 https://cloud.siliconflow.cn
2. 手机号注册(**送免费额度**)
3. 左侧 **API 密钥** → **新建**
4. 复制 Key

### 6.3 设置环境变量

**在同一个 PowerShell 窗口里**(注意:`.venv\Scripts\activate` 之后):

```powershell
$env:DEEPSEEK_API_KEY = "sk-你复制的key"
$env:ASR_API_KEY = "sk-你复制的key"

# 验证
if ($env:DEEPSEEK_API_KEY) { "DeepSeek Key 已设置" } else { "❌ 没设置上" }
```

**如果要永久生效**(重开终端也在):

```powershell
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-你的key", "User")
[Environment]::SetEnvironmentVariable("ASR_API_KEY", "sk-你的key", "User")
```

> 💡 **本项目会自动从注册表读 Key**,所以即使当前进程读不到环境变量也能工作。
> 这是为了躲开 Windows 上"设了变量但进程感知不到"的常见坑。

---

## 第 7 步:写配置

```powershell
copy config.example.json config.json
```

用记事本或 VS Code 打开 `config.json`,**至少改这三处**:

### ① 群名

```jsonc
"groups": ["把这里改成你的群名"]
```

**群名必须和微信里显示的完全一致**(包括空格、emoji)。

> 不确定群名?先随便填,启动后看日志里那行 `本机群名: A, B, C`,
> 把准确的复制过来。

### ② @ 触发名

```jsonc
"mention_names": ["小助手"]
```

群里 `@小助手` 就会触发。改成你想要的任何名字。

### ③ 保持干跑

```jsonc
"dry_run": true
```

**这一步先别改**,等第 10 步验证完再改。

### 可选:多账号机器

```jsonc
"wechat_account": "wxid_xxxxxxxxxxxx_xxxx"
```

不知道填什么就**留空**(自动检测)。多账号且自动检测挑错号时,看日志里的
`数据库已就绪,账号=...`,把那串填进来。

---

## 第 8 步:体检

```powershell
python run_bot.py check
```

**预期输出**:每项都是 ✓。

```
Python 版本         ✓ 3.12.x
依赖                ✓ 全部就绪
微信进程            ✓ 找到
配置文件            ✓ 已加载
群白名单            ✓ 1 个群
```

**任何一项 ✗ 都会附带怎么修。** 修完再跑一次,直到全 ✓。

---

## 第 9 步:离线演示

```powershell
python run_bot.py demo
```

这一步**不连微信、不花 API 钱**(用的是假数据和桩模型),只是让你看一遍决策流程。

**预期输出**:能看到它模拟处理几条消息,并打印"会不会回、为什么"。

**这一步是给你建立信心用的** —— 确认逻辑符合预期再去动真格。

---

## 第 10 步:干跑

**保持 `config.json` 里 `dry_run: true` 不变**,启动:

```powershell
python run_bot.py run
```

现在它会**真的读群消息、真的调模型,但不会发送**。看到日志里有:

```
读取 [你的群名] xxx
[干跑] 本想回复:「...」
```

说明**链路通了**。这时候去群里发 `@小助手 你好`,应该能看到它准备回什么。

**⚠️ 仔细检查这些"本想回复"的内容是否正常**:
- 有没有答非所问?
- 有没有太长(像小作文)?
- 语气对不对?

不正常就改 `config.json` 的 `persona`,改完重启再看。

> **建议至少观察 10~20 条再进下一步。**

---

## 第 11 步:正式运行

确认上一步的输出都满意后:

```jsonc
"dry_run": false
```

重启:

```powershell
python run_bot.py run
```

**现在它会真的在群里说话了。** 去发一条 `@小助手 你好` 试试。

**紧急停止**(随时):

```powershell
echo. > STOP     # 立刻停止回复
del STOP         # 恢复
```

---

## 第 12 步:开机自启(可选)

**放到 Windows 启动文件夹**:

1. `Win + R` → 输入 `shell:startup` → 回车
2. 在打开的文件夹里新建一个 `启动机器人.bat`:

```bat
@echo off
cd /d "C:\你的路径\wechat-ai-groupmate"
call .venv\Scripts\activate
python run_bot.py run >> bot.log 2>&1
```

3. 保存。下次开机就会自动启动。

**或者用任务计划程序**:触发器选"登录时",操作指向上面那个 bat。

> ⚠️ 自启前**务必先手动跑通**,否则开机就出问题不好排查。

---

## 验收清单

装完之后,逐条确认:

- [ ] `python run_bot.py check` 全 ✓
- [ ] 群名和微信里显示的一致
- [ ] `dry_run` 已改成 `false`
- [ ] 群里 `@小助手 你好` → 有回复
- [ ] 发张图 + `@小助手 这题怎么做` → 能看到图并回答
- [ ] 发条语音 + `@小助手` → 能听到并回答(需要 ASR Key)
- [ ] 日志里能看到 `[已发送]`
- [ ] `STOP` 文件能立刻叫停
- [ ] `config.json` 不会被提交到 Git(在 `.gitignore` 里)

---

## 出问题了?

1. **先看日志**:`bot.log`,或直接看终端输出
2. **再跑体检**:`python run_bot.py check`
3. **再看** [README 第 7 节 排错](README.md#7-排错) —— 常见症状都有对照表

**日志怎么看**:关键行是

```
读取 [群名] ...              ← 收到了
合并后的问题:「...」          ← 准备问模型什么
[已发送] 「群名」(xxx触发)    ← 真的发出去了
```

如果**只有"读取"没有"[已发送]"**,说明被冷却或护栏挡住了 ——
同一行日志会写明原因(比如"刚回复过"、"触发每分钟上限")。
