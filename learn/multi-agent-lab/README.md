# 多智能体实验场（Multi-Agent Lab）

这里不是又一篇文档，而是一个**能跑的实验室**。

上一版 `docs/01-面试八股文/06-多智能体.md` 之所以「读了一遍啥都没留下」，是因为它是**字典**：按知识分类罗列结论，你看到的全是别人的总结。这个 lab 换个做法——**让你亲手把多智能体系统做崩，再用 4 个版本一步步治好它**。

跑完之后，第 6 章那 9 节内容会变成你自己的经验，不需要背。

---

## 一、三步跑起来

```powershell
cd learn\multi-agent-lab

# 1) 装依赖（只有两个）
pip install -r requirements.txt

# 2) 配 Key：复制模板改成 .env，然后用编辑器填自己的 Key
Copy-Item .env.example .env

# 3) 跑（推荐先离线跑一遍看结构，不花钱）
python run_all.py --mock
python run_all.py
```

> `.env` 已经在仓库的 `.gitignore` 里，**不会被提交**。Key 只从环境变量读，永远不要写进代码。

---

## 二、你要配置什么（就这几项）

打开 `.env`，重点是前三项：

| 配置项 | 必填 | 说明 |
|---|---|---|
| `LLM_API_KEY` | ✅ | 你的 Key。本地 Ollama / vLLM 没有鉴权，随便填个非空串（如 `ollama`）即可 |
| `LLM_BASE_URL` | ✅ | OpenAI 兼容接口地址，**结尾是 `/v1`**，不要带 `/chat/completions` |
| `LLM_MODEL` | ✅ | 默认模型名 |
| `LLM_MODEL_FAST` | ⬜ | 执行类角色（analyst/architect/coder）用的小模型 |
| `LLM_MODEL_SMART` |  | 决策类角色（boss/reviewer/critic）用的大模型 |
| `LLM_PRICE_IN` / `LLM_PRICE_OUT` | ⬜ | 单价（元 / 百万 token）。填了才会把 token 折算成钱 |
| `LLM_TOKEN_BUDGET` | ⬜ | Guard 的第 ④ 道刹车：累计超过这个 token 数就停 |

### base_url 参考（**以各家官方文档为准**，我可能记错）

| 服务商 | `LLM_BASE_URL` | 模型名示例 |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 阿里百炼（通义） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b` |
| 本地 vLLM | `http://localhost:8000/v1` | 你部署的模型名 |

只要服务商支持 OpenAI 兼容协议，改 `LLM_BASE_URL` + `LLM_MODEL` 就能用，**代码一行都不用改**——这就是「依赖抽象」的价值。

---

## 三、四个版本，各治一个病

| 版本 | 结构 | 隐喻 | 治什么 / 暴露什么 |
|---|---|---|---|
| `v1_pipeline.py` | 流水线 + 共享黑板 | 工厂流水线 | 上下文被切短后每个 Agent 更聚焦；但错误会逐级传递，且**没法退回第一步** |
| `v2_boss_worker.py` | Boss-Worker（中心化） | 项目经理派活 | Boss 拆任务、质检、**带证据要求地打回重做**；代价是 Boss 成为单点 |
| `v3_deadloop.py` | 民主讨论**没有护栏** | 开会开不完 | 故意做崩：**死循环 + Token 肉眼可见地烧** |
| `v4_guarded.py` | 四道刹车 + 状态机 + trace | 给系统装安全阀 | 怎么检测死循环、怎么用证据让流程收敛 |

**建议按 1→2→3→4 顺序跑**，别跳。V3 一定要认真看完它怎么崩 —— 那一分钟是整个 lab 最值钱的部分。

---

## 四、每一版你要「看」什么

`v1_pipeline.py`
- 每个 Agent 的**入参**里，是不是只有「上一轮产物」？——这就是拆上下文。
- 想一想：如果把这三步塞进一个 prompt 给单个 Agent，会发生什么？那就是文档说的**注意力漂移**。

`v2_boss_worker.py`
- coder 第一次为什么被 FAIL？Boss 重派任务时**多加的那句话**是什么？
- 追问自己：如果 Boss 只写「请重新做一遍」，流程还会收敛吗？
- 看最后的 trace 日志：`FAIL → rework → PASS` 这条链，就是可观测性。

`v3_deadloop.py`
- 盯住每轮的 token 数：**它是线性增长、永不停下的**。
- 想清楚：哪个角色才是罪魁祸首？是 critic 太严格，还是**结构缺了三样东西**？

`v4_guarded.py`
- 场景 A：哪一道刹车最先响？
- 场景 A2：为什么「换措辞」就能让去重失效？——**真实的 LLM 每轮措辞都不一样**，这就是为什么生产上四道刹车都要装。
- 场景 B：真正的解法不是「刹车」，而是**把『必须附证据』写进派活指令**。

---

## 五、跑完回头看文档哪几节

| 文档《06-多智能体》 | 对应本 lab |
|---|---|
| 第 1 节 为什么需要多智能体 | V1 的「拆上下文」、V0 单 Agent 反例 |
| 第 2 节 三大协作模式 | V1 流水线、V2 Boss-Worker、V3 民主讨论 |
| 第 3 节 通信机制 | V1 的 `Blackboard` 类（共享黑板） |
| 第 4 节 任务分配 | V2 里 Boss 生成的 `subtasks` |
| 第 5 节 冲突解决 | V2/V4 的「证据门槛」与 reviewer 仲裁 |
| 第 6 节 状态管理与同步 | V4 的 `TaskState` 状态机 |
| 第 8 节 企业应用 | V2 的 FAST/SMART 模型分工 |
| 第 9 节 生产挑战 | V3（死循环 + 烧钱）、V4（四道刹车 + trace） |
| 附 Q14 死循环检测 | `Guard.RULES` 那四行 |
| 附 Q15 错误隔离 | V4 场景 B：校验 Agent 当门禁 |

---

## 六、面试会怎么用这些东西

跑完这四版，你就有了一段**可以讲的故事**，而不是背书：

> 「我们在多 Agent 流水线里发现两个问题：一是 critic 和 author 会无限打回重做，
> 二是模型每轮措辞都不一样，所以按文本去重根本拦不住。
> 我们的做法是分开处理：结构上让校验节点只认证据（要求附测试输出），
> 不认『已修复』这类声明；工程上加四道刹车，步数上限、状态去重、
> 无进展检测、Token 预算熔断。另外状态用显式状态机，非法迁移直接拒绝，
> 这样才可回放、可断点续跑。」

这段话里的每个字你都在代码里见过，所以讲出来是有底气的。

---

## 七、常见报错

| 现象 | 原因 |
|---|---|
| 一直走 mock、没调真实模型 | `.env` 不在 `multi-agent-lab` 目录下，或 `LLM_API_KEY` 为空 |
| `openai.AuthenticationError` | Key 错了 / 过期了 |
| `openai.NotFoundError` | `LLM_MODEL` 名字不对，或 `LLM_BASE_URL` 少了 `/v1` |
| `openai.APIConnectionError` | 网络或代理问题；本地服务先确认端口通了 |
| 报 `max_completion_tokens` 相关错误 | 代码已做兼容回退；若仍失败，换一个模型试 |
| V3 真的在烧钱 | 意料之中，它就是要让你看见这个。用 `--max-rounds 4` 限流 |

---

## 八、文件说明

```
multi-agent-lab/
├── README.md           本文件
├── requirements.txt    只有 openai + python-dotenv
├── .env.example        配置模板（复制成 .env 后填 Key）
├── lab_core.py         公共基础设施
│                       ├─ LLMClient   真实调用 / mock 兜底 / token 记账
│                       ├─ PERSONAS    六个角色的人设（system prompt）
│                       ├─ Tracer      全链路留痕
│                       ├─ Guard       四道刹车
│                       └─ TaskState   状态机
├── v1_pipeline.py       流水线 + 共享黑板
├── v2_boss_worker.py    Boss-Worker
├── v3_deadloop.py       故意做崩的死循环
├── v4_guarded.py        刹车 + 状态机 + trace
└── run_all.py           一键按顺序跑完 V1~V4
```
