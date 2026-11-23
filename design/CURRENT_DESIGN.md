# Alpha Research 系统整体设计

> **当前设计入口（2026-11-23）**。本文描述功能分支中的系统能力、依赖、信任边界和实施状态，不是授权令牌、生产就绪证明或新阈值来源。
>
> **规范优先级**：项目 [AGENTS.md](../AGENTS.md) 和已批准的 [RESEARCH_SCOPE.md](RESEARCH_SCOPE.md) 控制研究范围与权限；代码及实际配置决定接口行为；运行结果须以与提交 SHA 绑定的 CI/平台/供应商证据为准。发生冲突时不得由本文扩大已批准范围。

## 1. 产品能力与总体原则

`alpha_research` 是围绕 WorldQuant BRAIN 的研究、评估、资格审计和恢复系统，覆盖三种**业务工作流**：

| 工作流 | 核心输入 | 研究机制 | 不变量 |
| --- | --- | --- | --- |
| Regular Campaign | Region，研究目标；可选 Dataset、Category、Universe | 按当前平台证据发现合法组合、筛选字段、预注册、模拟与审计 | Regular Delay 1；未覆盖的 Dataset/组合必须记录；单表达式不得跨 Dataset |
| BRAIN-native Super Alpha | Region、目标、平台支持的 Super 设置 | 使用平台原生选股、组合和 Super 仿真契约 | 不从 Regular 推断 Super 的有效数据与机制 |
| Alpha Optimization | 源 Alpha ID、目标、可验证的源设置 | 读取源对象只读基线，独立分支进行研究与审计 | 不修改原 Alpha；源对象类型和范围由平台证据确认 |

PPA 只是用户**显式要求**时应用于 Regular 的特殊 Profile，不是另一条工作流。只有充分验证的平台读回与完整适用的 RA/相关性/指标规则才能产生 Candidate；Preferred 在 Candidate 集上独立优选。局部 PPA 判断不等于平台 PPA 分类。**Alpha 自动提交禁止**。

系统的运行原则是：范围先于模型决策，意图先于非幂等调用，证据先于结论，失败/未知先于重试，执行权先于平台副作用。

## 2. 架构总图：业务、平台与控制平面

```text
CLI / Workbench / Operator
     |
     +--> Configuration + Role Registry -----------------------+
     |     Lead / Executor -> Agent CLI | SDK Agent | LLM API   |
     |                                                        v
     +--> MainOrchestrator / Task checkpoints ---------> Model runtime
     |      identity / budget / preparation / recovery       |
     |      |                                                 | hypotheses & decisions
     |      v                                                 v
     |   Workflow dispatcher <--------------------------------+
     |      +--> Regular Campaign / Dataset tasks
     |      +--> Super-native workflow
     |      +--> Optimization branch workflow
     |                   |
     |     Evidence / expression / simulation-intent
     |                   |
     |                BRAIN Gateway -> isolated Driver -> BRAIN
     |                   |            (separate authorization boundary)
     |     Platform readback + deterministic qualification
     |                   |
     |        Candidate / Preferred / audit & reports
     |
     +--> Research control plane (read-only reports)
              |
              +--> Grant / independent authority reference service
              +--> Provider attempt & micro-USD spend ledger
              +--> Signed session / scoped Catalog read audit
              +--> Canary monitor / irreversible Stop
              +--> Cross-module negative release matrix

State root: versioned records, evidence fingerprints, intents, journals,
            single-writer locks, recovery and auditable results
Knowledge:  Dataset / field / operator snapshots and research references
```

### 模块边界与代码定位

| 层 | 代码主体 | 负责 | 不负责 |
| --- | --- | --- | --- |
| 入口 / 装配 | `cli.py`, `workbench/`, `config.py` | 命令、角色装配、显式配置和可见状态 | 自行设定 Alpha 资格或代替外部授权 |
| 模型角色与治理 | `agents/`, `integrations/model*.py` | 已注册 Agent/LLM 选择、预算、提示、调用日志与响应校验 | 持有 BRAIN 真实权限或决定 Candidate |
| 编排 / 控制 | `orchestration/engine.py`, `dispatch.py`, `research_*.py` | Task、检查点、证据交接、授权审计、事务和恢复协调 | 绕过领域 Intent 重新执行 Simulation |
| 业务领域 | `workflows/regular/`, `super_alpha/`, `optimization/` | 各自研究范围、生成和执行协议 | 创建第二套权限发行与账本 |
| 平台适配 | `integrations/brain/`, `simulation/` | Catalog、精确范围、Driver 协议、Intent、读回 | 以模型文本或本地报告代替 BRAIN 事实 |
| 纯确定性逻辑 | `expressions/`, `qualification/` | 语法规则、Candidate、Preferred、PPA 和相关性核验 | 未见证据时默认成功 |
| 状态与知识 | `state/`, `knowledge.py` | 原子持久化、证据定位、只读快照、锁与日志 | 把普通 SHA-256 当成远程数字签名 |

`MainOrchestrator` 采用注入 Adapter 的方式，默认不启用真实执行；独立 `ProductionWorkflowAdapter` 仍会在前置校验阻断生产 Dispatch。既有领域 CLI 入口与新编排控制面是不同路径：前者具有按命令独立检查的执行契约，不能被「统一门禁未放开」误写为所有命令物理上不可能访问 BRAIN。任何真实调用均需确认具体入口的权限和副作用。

## 3. 研究、模型与资格的端到端证据链

1. **Scope**：记录用户约束、Region、Dataset/源 Alpha、适用平台设置和精确时间范围；用户给定条件不得静默替换。
2. **Platform Catalog**：使用现行账号、Region/Delay/Universe/Instrument 证据核对 Dataset、字段与操作符，并保存快照及摘要。Regular 缺省 Dataset/Category/Universe 代表覆盖义务，不代表任意合法化。
3. **Hypothesis/Plan**：Lead 给经济假设和研究目标，Executor 生成可验证计划、表达式和属性推理；两者必须绑定角色、Backend、输入摘要和 Research/Task 身份。
4. **Preparation & validation**：表达式、字段、操作符、参数和预算在任何仿真 Intent 前验证；保留预注册研究证据，禁止用模型建议填造平台结果。
5. **Intent & authorization**：先持久化模拟 Intent、审批引用与准确范围；只有独立可信签发/验证/预算/单次消费机制以及该入口的独立许可才可能考虑真实非幂等 POST。现有新研究控制面继续 **fail closed**。
6. **Readback & reconciliation**：严格绑定返回的请求/operation/Alpha 身份；丢失 ACK 或未知结果隔离为 UNKNOWN，先协调读回，不自动重放。
7. **Qualification**：适用 RA 要求通过后才进入进一步相关性及指标核验；缺失、失败、无法读取的证据不得作为通过。Candidate/Preferred/PPA 分开记录。
8. **Report**：写入候选、失败原因、未覆盖范围、费用和血缘信息；报表状态不是额外的执行许可。

研究决策层四种边界必须分开：Workflow Scope（业务流程类型）、Research Scope（用户/平台有效范围）、Research Profile（如显式 PPA）、Execution Policy（批准的权限与预算）。`Research Lead` 与 `Research Executor` 是角色，Codex/Antigravity/Claude 等是 Agent Runtime，DeepSeek/GLM/Kimi 等是注册的模型服务；**角色、执行客户端与模型服务三者不能混淆**。

## 4. 模型服务与费用治理

`agents/model_registry.json` 维护可用后端预设，`agents/role_profile.json` 为角色配置；启动时 `--lead` 和 `--executor` 指定已注册后端。可能的传输类型包括 CLI Agent、SDK Agent、OpenAI Python SDK 兼容接口及确定性 Mock。模型密钥仅可从已注册的环境变量读入，不得写入仓库、Task、状态或日志。调用前再次核对 Provider 主机、API 路径、配置身份与请求输出上限；不允许凭响应文本升级权限。

预算至少在 Task / Research / Model Attempt 和全局 Provider 层保持一致的身份、调用次数与保留费用；费用采用整数 micro-USD 上取整估算，不把本地 token 估计当成真实账单。模型调用/网络 ACK 不明时保持预算保留及 `UNKNOWN`，不得通过自动切换 Backend、自动退款或重试规避限制。

`research_p7_billing.py` 仅对**调用方提供**的发票行和保留记录做完整集合匹配、费用差额及超限审计；没有 Provider 独立认证就不能宣布结算。统一状态词表由 `research_p7_contracts.py` 管理，跨模块验收位于 `research_p7_release.py`。

## 5. 权限、密钥、BRAIN 只读与生产发布边界

### 控制平面与独立 Authority

`research_grant_service_reference.py`、`research_spend_service_reference.py` 和 `research_authority_*.py` 实现单次 Grant 消费、签名 ACK、SQLite 事务化费用保留、审计读回、恢复和持久的 Global Stop。独立运行的**本地参考服务**可以演练 mTLS、身份与崩溃恢复，但不是已完成的生产部署。

`deploy/p7/` 提供**仅 Linux host-network/loopback** 的隔离容器配置，默认非 root、只读根文件系统、mTLS、独立的 API/admin token 文件和可持久化 SQLite。真正的 CA/签名私钥、证书轮换、机主身份、备份恢复、告警及 on-call 责任都需要外部独立配置。

### BRAIN 边界

BRAIN Gateway 与 Driver 对任务、批准研究范围、Catalog 命令白名单和 Session/ACK 绑定进行二次校验。只读 `research_p7_brain_read.py` 验证已签名的本地 Session 观察、框架摘要、Authority Stop 和调用方报告的读回血缘，**不登录平台、不启动真实研究 Driver、不独立认证响应，也不授权任何 BRAIN 请求**。Simulation POST、溯源标签写入和 Alpha 属性操作各自需要不同的操作权限；自动 Alpha 提交依项目政策禁止。

### Canary / emergency Stop / release

`research_p7_canary.py` 审核监控新鲜度、UNKNOWN、全局预算压力、不可逆 Stop、计数器回退及账单/BRAIN 证据漂移；返回停止建议，但不会自动调用 admin Stop。真实 Stop 需要独立受控的 admin 通道，不能靠本地文件/伪造日志撤销。

`research_p7_release.py` 与上层 `release_gate.py` 的唯一正当结论是本地审计状态和真实生产阻断项；当前版本统一 **`P7_PRODUCTION_BLOCKED`**，所有生产 Dispatch、模型、BRAIN 只读、Simulation、Alpha 提交及自动重试授权标志为 `false`。即使本地账单完全匹配、Session 摘要有效、Canary 状态 ARMED，也不会变成真实生产授权。

发布前不可由仓库代码单方面解决的条件：

- 在独立生产主机部署和审计 mTLS/PKI/密钥托管、Grant、预算、审计存储、恢复、告警和运维责任；
- 从 Provider 独立认证真实 operation/readback、Tokens、发票、价格版本和硬扣费上限；
- 在 BRAIN 端独立认证可撤销、精确 Scope 的真实只读 Session 与请求/响应读回；
- 由授权操作者监督真实 Canary，验证从 Stop 到各真实执行端点的传递；
- 对任何 BRAIN Simulation POST 获取另外的限额/范围授权；Alpha 自动提交继续禁止。

详细的运维与证据矩阵参见 [运行手册](../docs/OPERATIONS.md) 和 [验收与发布](../docs/RELEASE_ACCEPTANCE.md)。

## 6. 持久化、重入和恢复语义

- `MainOrchestrator` 的 Task 状态与 `validate → execute → qualify → collect` 检查点必须在持久化层确认；Task 完成不等于 Candidate 合格。
- Regular Campaign 的 Dataset/组合/批次，Super/Optimization 的模拟 Intent，模型 Attempt、授权消费和独立费用账本为**不同状态机**；不能为了复用接口合并为单一可重试状态。
- 非幂等请求前持久化 Intent，返回未知时留在 UNKNOWN/blocked。恢复凭原请求和已验证的独立读回/收据进入，不能替换收到过但不可核实的结果。
- `state/fingerprint.py` 的普通 SHA-256 只用于检测普通修改；攻击者可改写本地文件时不能视为远程真实性证明。
- 状态数据写入配置的 state root；仓库共享 `knowledge/` 保存按 Scope/账号约束的 Catalog 快照。原子保存、单写锁、文件路径信任、跨进程占用及多次调用账本须保留 OS 差异与故障恢复语义。

详见 [State 模型](RESEARCH_STATE_V1_1.md)、[重构/并发边界](RESEARCH_REFACTOR_BOUNDARIES.md) 和 [统一调度](MAIN_ORCHESTRATOR.md)。

## 7. 真实实施状态与验收证据

| 维度 | 代码/离线状态 | 尚未证明 |
| --- | --- | --- |
| 三类研究工作流及 Mock 流程 | 代码与离线测试覆盖，包含 Candidate / PPA / recovery 规则 | 各种真实 BRAIN 凭证、仿真及可提交能力 |
| Agent/LLM 路由 | 预设 Registry、Role、治理、SDK Mock 与跨平台回归 | 真实 Provider 用量/价格/收费合规 |
| 审计与参考 Authority | 本地签名、账本、mTLS 与一次性 Stop 演练 | 独立部署、生产证书与服务端执行权 |
| BRAIN read audit | 有签名观察和纯本地 Frame 校验 | 独立 BRAIN Session 认证/真实读回 |
| 费用、Canary 与发布门禁 | 完整报告校验、状态决策、持久阻断 | Provider 官方发票、监督真实 Canary、上线批准 |

截至 `e0f76f9` 提交，GitHub Actions [主流程离线验收](https://github.com/Justices/alpha_explore_agent/actions/runs/45188802861) 五个 Job 和 [隔离 Authority 验收](https://github.com/Justices/alpha_explore_agent/actions/runs/45188802903) 三个 Job 全部成功；Ubuntu 全量回归为 **959 tests, OK**。这是**与该提交绑定的离线证据**，不证明真实 BRAIN/Provider 部署。之后的修复应在对应提交的 CI 结束后再补入验收结论。

## 8. 统一文档结构

| 文档 | 用途 |
| --- | --- |
| [AGENTS.md](../AGENTS.md) | 项目行为、安全与代理契约（规范性） |
| [RESEARCH_SCOPE.md](RESEARCH_SCOPE.md) | 用户批准的工作流、Scope、PPA、Candidate/Preferred 规则（规范性） |
| **本文** | **现行整体架构、真实状态、关键设计和唯一主导航** |
| [RESEARCH_ARCHITECTURE_V1_1.md](RESEARCH_ARCHITECTURE_V1_1.md) | Package 依赖关系与适配器边界细节 |
| [MODEL_AGENT_REGISTRY.md](MODEL_AGENT_REGISTRY.md) / [MODEL_RUNTIME_GOVERNANCE.md](MODEL_RUNTIME_GOVERNANCE.md) | 注册、路由、费用治理、失败语义 |
| [RESEARCH_STATE_V1_1.md](RESEARCH_STATE_V1_1.md) / [RESEARCH_REFACTOR_BOUNDARIES.md](RESEARCH_REFACTOR_BOUNDARIES.md) | 状态数据结构、文件锁、恢复及路径边界 |
| [MAIN_ORCHESTRATOR.md](MAIN_ORCHESTRATOR.md) | 任务状态机与领域恢复映射 |
| [OPERATIONS.md](../docs/OPERATIONS.md) | 只读日常运维、容器参考演练、独立部署事项 |
| [RELEASE_ACCEPTANCE.md](../docs/RELEASE_ACCEPTANCE.md) | 统一离线验收及外部生产阻断矩阵 |

旧的迭代日志与阶段性说明将归档，不再作为当前系统设计的主入口。任何新模块都应更新本文对应**架构职责**，而不是新建同义的阶段文档。
