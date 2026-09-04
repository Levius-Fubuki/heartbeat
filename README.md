<h1 align="center">Heartbeat</h1>

<p align="center"><strong>一个会主动找你的桌面陪伴 agent。</strong></p>

<p align="center"><sub>Not another chatbot that waits for you to speak — a companion that checks on you first.</sub></p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS-lightgrey.svg">
  <img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-yellow.svg">
  <img alt="LLM" src="https://img.shields.io/badge/LLM-OpenAI%20compatible-7c3aed.svg">
  <img alt="Status" src="https://img.shields.io/badge/status-alpha%20·%20active%20R%26D-orange.svg">
</p>

<p align="center">
  <img src="personas/image/zorya/zorya-idle.png" width="96" title="Zorya 晨星" />
  <img src="personas/image/vesna/vesna-idle.png" width="96" title="Vesna 初春" />
  <img src="personas/image/rada/rada-idle.png" width="96" title="Rada 喜悦" />
  <img src="personas/image/yara/yara-idle.png" width="96" title="Yara 锋利" />
  <img src="personas/image/mira/mira-idle.png" width="96" title="Mira 宁静" />
</p>

---

## 它和别的 AI 有什么不同

市面上几乎所有的 AI 聊天——ChatGPT、Claude、Replika、Character.AI,甚至 43k★ 的开源项目 [airi](https://github.com/moeru-ai/airi)——都是**被动式**:你不说话,它就沉默。

Heartbeat 反过来。它像一个真正在场的伙伴:周期性「心跳」感知你的电脑状态与时间,**在你需要的时刻主动出现**,在你专注时安静退开。重启之后,它还记得你上次在干嘛、你习惯几点写代码、你跟它说过什么。

| 能力 | Heartbeat | airi (43k★) | Replika / Pi | ElliQ | ChatGPT |
|---|:---:|:---:|:---:|:---:|:---:|
| **主动发起陪伴** | ✅ 心跳 | ❌ 反应式 | ❌ | ✅(硬件) | ❌ |
| **多人格 + 记忆隔离** | ✅ | ❌ | ❌ / 弱 | ❌ | ❌ |
| **分层记忆 + 归纳触发** | ✅ L0–L4 | ⚠️ WIP | 云端黑盒 | 闭源 | ❌ |
| **桌面上下文感知** | ✅ | ❌ | ❌ | N/A | ❌ |
| **本地优先 / 隐私有界** | ✅ | 部分 | ❌ | ❌ | ❌ |
| **形态** | 软件 · macOS | 多平台 | 手机/网页 | 硬件 | 软件 |

> 唯一同样「主动」的 shipping 产品是 ElliQ——但它是**硬件 + 老年护理 + 单人格**。Heartbeat 用纯软件感知 + LLM 判断替代了硬件在场:更难、也更少有人做。

---

## ✨ 核心特性

💓 **主动式心跳陪伴** · 每 60s 采样前台 App / 空闲 / 时间。命中情境才开口;不该打扰时走招牌 `alert → settle` 动画——抬头看一眼,确认你正忙,再无声趴回去。

🎭 **5 个斯拉夫神话人格** · Zorya 晨星 / Vesna 初春 / Rada 喜悦 / Yara 锋利 / Mira 宁静。**记忆按人格隔离**:Vesna 看不到你跟 Zorya 说过的秘密;切换人格时,新人格凭关系设定**即兴吐槽**上一个(看不到对话内容,只知道你刚和谁待过)。

🧠 **L0–L4 五级分层记忆** · 实时变化检测 → 工作记忆 → 用户档案 → 情景向量召回(本地 bge-small-zh)→ **行为模式归纳**。L4 会归纳你的作息(「深夜常边写代码边听音乐」),既让 agent 懂你,又**反过来成为主动搭话的触发源**——记忆闭环驱动主动行为。

🌅 **Dawn 晨观台 UI** · 深夜天幕 + 人格色「黎明日」光晕 + 极淡星点颗粒 + 西里尔衬线人格名(Зоря)。每个状态一张像素图(16-bit,仅面部裁切);切换人格,整套配色随之变换。

🔒 **本地优先 · 隐私有边界** · 你的档案 / 对话 / 作息全部落在本地 `memory/`,不上云、不进 git。API key 只在 `.env`。窗口标题默认只在本地分类,**绝不**直发云端。

🛠️ **ReAct + 逐次授权** · agent 在「决定要说话」时才进入 ReAct,可先调工具(`get_activity` / `recall_memory` / `get_time`)再回答。任务模式下每一步操作都**逐次授权**——不是 Open Interpreter / Computer Use 那种全自动。

---

## 🎬 演示

> 截图 / GIF 位(待补):心跳主动搭话 · 切换人格即兴吐槽 · 桌宠就地输入 · 跨对话记忆召回

- 打开 Spotify → 它抬头:「切首歌换换节奏?」
- 深夜 1 点还在 Warp → 它:`alert` 看一眼 → 判定你正专注 → `settle` 无声趴回
- 问 Vesna「记得我昨天说的事吗」→ 它查 L3 向量召回,只在自己的人格记忆里找

---

## 🚀 快速开始

```bash
git clone https://github.com/Levius-Fubuki/heartbeat.git && cd heartbeat
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # 填入 HEARTBEAT_LLM_API_KEY(必需;DeepSeek/智谱/OpenAI/Ollama 均可)
.venv/bin/python main.py    # 打开透明桌宠 + 自动弹出主窗口
```

首次启动选一个人格(5 选 1),之后它就记得你了。也可启动后在主窗齿轮 ⚙ 设置页直接换模型 / 凭据 / 心跳间隔(免重启热生效)。

---

## 🧠 记忆系统

```
L0  实时状态缓存    滑动窗口 + 变化检测(Heartbeat 特有,桥接感知 ↔ 记忆)
L1  工作记忆        当前对话                       ──┐ 按人格隔离
L3  情景记忆        对话归档 + 本地向量召回          ──┘
L2  用户档案        你亲口说的性格/爱好/作息 → 始终注入 ──┐ 跨人格共享
L4  行为模式        agent 观察归纳的作息 → 注入 + 主动触发 ──┘
```

**隔离边界**:L1 / L3 按人格隔离(Vesna 看不到 Zorya 的对话);L2 / L4 跨人格共享(描述的是同一个你)。划分依据是**来源**——L2 是你声明的,L4 是 agent 观察的。

---

## 🔒 隐私

| 探针 | 取到 | 权限 | 发云端? |
|---|---|---|---|
| 前台 App | App 名 + bundle id | 无 | 仅类别 |
| 空闲 | 距上次键鼠秒数 | 输入监控 | 秒数 |
| 窗口标题 | 焦点窗口标题 | 辅助功能 | **绝不**(只本地分类) |
| 时钟 | 小时 / 深夜 / 清晨 | 无 | — |

**看不到**:后台 App、其他窗口/标签页、网页正文、URL、文件内容、剪贴板、屏幕像素、键鼠输入。所有记忆与 key 留在本地。

---

## 🗺️ 路线图

- ✅ **Phase 0–4+** 骨架 / 真实 macOS 探针 / 记忆 L2–L4 / ReAct + 向量召回 / 5 人格视觉与记忆隔离 / 流式输出 / Dawn 晨观台 UI / 设置页(运行时换模型) / 任务模式 + 逐次授权
- 🔜 更多探针(Process / Screenshot / Clipboard 本地脱敏)· STT 推到说 · 打包分发

<details>
<summary><b>📦 项目结构</b></summary>

```
heartbeat/
├── main.py            # 入口:NSApplication 起 uvicorn 线程 + 透明桌宠 + 主窗
├── backend/
│   ├── config.py      # 时序/LLM/记忆/ReAct/embedding 常量(均可 env 覆盖)
│   ├── ws.py          # 多客户端 WS hub
│   ├── heartbeat.py   # 心跳循环 + 状态机 + 归档/抽取 + ReAct 注入 + 按人格隔离的 L1/L3
│   ├── app.py         # FastAPI:静态前端 + /ws + lifespan 预热 embedding
│   ├── approval.py    # 任务模式逐次授权
│   ├── voice.py       # TTS(edge/azure/cosyvoice)
│   ├── perception/    # 真实探针:ForegroundApp / Idle / WindowTitle / Clock
│   ├── judgment/      # rules(9类+冷却) / patterns(L4 触发) / sandbox(任务模式)
│   ├── memory/        # l0 / l2 / l3(向量召回) / l4(归纳) / embeddings / settings
│   └── llm/           # gateway(judge/react/extract) / tools(ReAct 三工具)
├── frontend/          # pet.*(透明桌宠) / chat.*(主窗 Dawn) / input.*(就地输入) / fonts/
├── shell/             # app.py(NSApp+AppDelegate) / window.py(Pet/ Main/ Input 三窗)
├── personas/          # 5×.yaml(神话内涵 + 20 条关系矩阵) + image/<persona>/<state>.png
├── prompts/           # onboarding 两段式
├── scripts/           # 各类 probe(管道验证)
└── memory/            # 运行期数据(.gitignore,留 .gitkeep)
```

</details>

<details>
<summary><b>🧪 管道验证(开发者)</b></summary>

```bash
.venv/bin/python scripts/rule_probe.py        # 9 类规则 + 冷启动回归(13 断言)
.venv/bin/python scripts/embed_probe.py       # L3 向量召回语义 + 老 entry 迁移补 id
.venv/bin/python scripts/react_probe.py       # ReAct WS 端到端
.venv/bin/python scripts/pattern_probe.py     # L4 模式:trigger 匹配 + 归纳 + 端到端
.venv/bin/python scripts/switch_probe.py      # 人格记忆隔离 + 切换吐槽(8 case)
.venv/bin/python scripts/interview_probe.py   # onboarding Stage 2 首次访谈
.venv/bin/python scripts/prompt_probe.py --mode react_reply --text "你在干嘛"   # 秒级验证
```

</details>

---

## 📜 License & 致谢

MIT — 见 [LICENSE](LICENSE)。人格像素图由 [nano-banana](https://github.com/MoonshotAI) 生成。

受 Claude Code 的记忆分级、MemGPT 的记忆管理,以及「主动式陪伴」(proactive companionship)这一尚未被充分探索的方向启发——尤其 ElliQ 证明了这条路存在,而 Heartbeat 想用纯软件再走一遍。
