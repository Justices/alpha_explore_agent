# 当前系统设计总览（Current Design）

> 状态：当前功能分支的设计导航与实现核对记录。本文为**说明性文档**，不是新的权限、数值阈值或平台事实来源。
> 规范优先级：根目录 [AGENTS.md](../AGENTS.md) 与已批准的 [研究范围](RESEARCH_SCOPE.md) 优先；具体实现行为必须与代码、配置和可验证的运行证据核对。

## 1. 目标、范围和完成边界

`alpha_research` 是 BRAIN Alpha 研究与评估系统，支持：
- **Regular Alpha Campaign**：明确 Region，按 Dataset/有效范围组合研究；PPA 是显式开启的 Regular Profile，而非第四种工作流。
- **BRAIN-native Super Alpha**：使用平台原生 Super 机制和平台确认的输入集合；不套用 Regular Dataset 约束。
- **Alpha Optimization**：给定源 Alpha ID，读取原始 Regular/Super Alpha 的只读基线，创建独立候选分支，不修改源 Alpha。

系统将研究计划、模型决策、模拟意图、读回证据、资格审计和恢复状态持久化。**模型输出不能替代平台证据或确定性的资格门槛。**

当前禁止自动 Alpha 提交和未经授权的属性更新；Regular 的已验证模拟结果可依现有契约处理研究溯源标签。任何真实 BRAIN Simulation、标签写入、模型收费调用必须在各自的显式授权边界内运行。

## 2. 架构及责任边界

```text
CLI / Workbench (入口和装配，不决定研究结论)
  |-- model selection -> Role Profile + Model Registry
  |                        |-- Research Lead
  |                        |-- Research Executor
  |                        '-- Agent CLI / SDK 或 LLM API Adapter
  |-- orchestration       -> 持久检查点、预算、执行与恢复协调
  |-- workflows           -> regular / super_alpha / optimization
  |-- integrations        -> BRAIN Gateway / 模型调用和审计
  |-- simulation          -> Intent、读回、恢复、回测缓存
  |-- qualification       -> Candidate / Preferred / 可选 PPA
  |-- knowledge           -> 规则、字段、操作符及证据检索
  '-- state               -> 文件存储、指纹、互斥和恢复日志
```

**权责划分**：
- 工作流拥有 Scope 决策和研究步骤；统一调度器负责阶段检查点和恢复协调，不能越权调用 BRAIN。
- BRAIN Gateway 负责平台会话、事实读取与被授权操作；模型 Provider 不拥有 BRAIN 工具和 Simulation 提交权限。
- 验证、Candidate/Preferred、相关性规则属于可审计的确定性逻辑；模型只能提出假设、构造、解释与建议。
- `MainOrchestrator` 当前默认拒绝真实执行；现有直接 CLI 工作流与统一任务编排是不同的执行入口，不能将离线编排测试当成真实 BRAIN 集成通过。
- 统一调度和已有领域流程保留各自检查点/恢复语义，禁止因结构整合而自动重放结果未知的请求。

模块级边界详见 [架构](RESEARCH_ARCHITECTURE_V1_1.md)；并发、锁、日志与恢复的细节详见 [重构边界](RESEARCH_REFACTOR_BOUNDARIES.md)。

## 3. 模型注册、路由与凭证

- `alpha_research/agents/model_registry.json` 为默认模型注册项；可由 `--registry` 或 `<state-root>/config/model_registry.json` 覆盖。
- `alpha_research/agents/role_profile.json` 统一定义 Research Lead、Research Executor 和任务路由；`--lead`、`--executor` 必须分别选定已注册的 Backend。
- Agent Runtime（例如 Codex、Claude Code、Antigravity）和 LLM API（例如 DeepSeek、GLM、Kimi）是**不同传输类型**，不是业务角色。LLM API 当前使用兼容 OpenAI Chat Completions 的官方 Python SDK。
- 根据现行 `AGENTS.md`，模型 API Key **只允许从配置指定的环境变量读取**，不能在提交的配置文件、调用日志或状态快照中保存明文密钥。
- 显式指定角色时，CLI 从权威 Role Profile 构建路由，旧业务 `model` 字段不能覆盖已选注册项。
- `models inspect` 与 `models doctor` 为本地检查；`READY_LOCAL` 不是远程鉴权成功。`models smoke --allow-model-call` 会真实调用模型，但不调用 BRAIN。
- 模型调用账本预留调用额度、记录服务商报告的 Tokens、绑定 Role/Backend 身份和研究 ID；未知结局不自动重试、不自动切换模型。

详见 [注册与选择](MODEL_AGENT_REGISTRY.md)、[模型治理](MODEL_RUNTIME_GOVERNANCE.md)。

## 4. 三类工作流的证据链

### Regular

`Scope -> 有效 Dataset/组合 -> 字段/操作符准入 -> 假设和预登记 -> Simulation Intent -> 授权 POST -> 平台读回 -> 完整资格审计 -> 结果记录`

Regular Campaign 遵循已批准的 Delay 1、Dataset 轮换与预算规则；省略 Dataset/Category/Universe 触发基于平台证据的覆盖义务，不代表可以编造范围。字段池最多 100 个，须持久化并用于恢复。PPA 规则仅在显式要求时启用。

### Super

从平台 Super 机制读取支持的设置、候选 ACTIVE Alpha 集合，执行原生构造和适用的模拟/审计。任何缺失的机制能力或证据都标记未决，不以 Regular 语义替代。

### Optimization

读取原始 Alpha 的类型与设置，保持源对象只读；独立记录候选分支、差异、有效设置、模拟意图、读回与审计。结果未知的请求先走恢复，不得再次 POST。

三者最后进入统一 `CandidateQualificationService`。Candidate 必须满足完整证据和适用的 RA、相关性、指标检查；**只有 `CANDIDATE` 才进入 Preferred 评估**。Preferred 不修改 Candidate 判定、不自动提交。全球候选池为从版本化评估记录衍生的视图，不是新的审批源。

## 5. 持久化、恢复与安全不变量

- BRAIN 目录快照位于仓库 `knowledge/`；其余运行数据默认存于独立 `state-root`；不得将本地凭证、Cookie 或密钥纳入仓库。
- 每次模型决策绑定精确的研究 ID、角色、后端、任务、输入摘要、预算和审计记录；批次和 Session 索引必须一致。
- 模型请求、Simulation 请求都可能在写入/回包之间中断。对结果不明的操作保留 UNKNOWN/阻断状态，只有具备可核验收据的显式协调才能推进。
- Task、恢复目标、研究调用和账本保持各自粒度的跨进程锁；失败不能隐式改判为成功或重新扣用未知额度。
- 文件引用、哈希和安全路径校验提供审计完整性，但不是针对有权限重写本地文件的攻击者的数字签名；Windows 文件竞争保护有已知的 best-effort 限制。

具体数据结构详见 [研究状态](RESEARCH_STATE_V1_1.md)；重试、锁、调用记录及路径信任边界详见 [重构边界](RESEARCH_REFACTOR_BOUNDARIES.md)。

## 6. 已验证事实与尚未通过的发布关口

| 项目 | 状态 | 证据边界 |
| --- | --- | --- |
| 双角色 DeepSeek Smoke | 已获得操作者本地 `PASS` 报告 | 两次 `model_probe_v1` 请求成功，未调用 BRAIN；不代表完整研究成功 |
| 显式模型选择与旧业务配置冲突修复 | 功能分支已提交 | Python 自动测试结果仍需核验 |
| Doctor 细粒度本地诊断 | 代码已提交 | 已静态核对，需运行离线回归 |
| 离线模型路由、交接和三类工作流测试 | 测试代码已提交 | 未取得本地 unittest/CI 通过证据 |
| BRAIN 只读准入与真实工作流全链路 | 未验证 | 必须单独执行经授权的只读预检 |
| 真实 Simulation、恢复和标签写入 | 未授权且未验证 | 不得因模型 Smoke 通过而执行 |
| 自动 Alpha 提交 | 不支持 | 不属于当前产品边界 |

不要把“代码已实现”“测试已编写”“本地测试通过”“真实平台验证通过”视为同一个状态。

## 7. 建议的推进次序

1. 在 Windows 项目虚拟环境中执行离线单元与集成测试，修复失败并核查不发生真实调用。
2. 审计 Session/模型预算/模拟 Intent 的恢复一致性，并做离线故障注入。
3. 在获得独立授权后进行 BRAIN 只读准入测试。
4. 在明确 Simulation 数量、成本和权限并单独授权后执行受控真实研究。
5. 完成最终 Review、证据核验，再决定是否合并功能分支。

受控上线步骤详见 [P0 就绪手册](P0_LIVE_READINESS.md)。

## 8. 现行设计文档索引

| 文档 | 唯一职责 |
| --- | --- |
| [AGENTS.md](../AGENTS.md) | 最高优先级的项目行为、安全和研究代理契约 |
| [RESEARCH_SCOPE.md](RESEARCH_SCOPE.md) | 已批准的产品边界、范围、Candidate 与 Preferred 规则 |
| 本文 | 现行架构总览、实现与证据状态、阅读入口 |
| [RESEARCH_ARCHITECTURE_V1_1.md](RESEARCH_ARCHITECTURE_V1_1.md) | 包和组件职责、依赖方向 |
| [RESEARCH_STATE_V1_1.md](RESEARCH_STATE_V1_1.md) | Campaign、分支、状态和证据数据模型 |
| [RESEARCH_REFACTOR_BOUNDARIES.md](RESEARCH_REFACTOR_BOUNDARIES.md) | 并发、调用账本、重放、文件信任和恢复协议 |
| [MODEL_AGENT_REGISTRY.md](MODEL_AGENT_REGISTRY.md) | 注册格式、模型/Agent 选择与传输 |
| [MODEL_RUNTIME_GOVERNANCE.md](MODEL_RUNTIME_GOVERNANCE.md) | Doctor、预算、身份固定与未知结局 |
| [P0_LIVE_READINESS.md](P0_LIVE_READINESS.md) | 运维预检、分阶段授权和验收手册 |

曾有的三份 `AGENT_REQUIREMENTS.md`、`AGENT_DESIGN.md` 与 `AGENT_KNOWLEDGE_DESIGN.md` 属于已被现行范围/架构/知识实现替代的原始设计，不再作为当前分支的维护入口；若需考证历史，可从 Git 提交历史中读取。
