# 设计—代码一致性审计（2026-11-20）

> 审计范围：`refactor/configurable-codex-agy-runtime` 功能分支的关键代码与现行设计文档；**静态代码审计**，不是自动化测试通过声明，也不是 BRAIN 生产验收。遵循 [AGENTS.md](../AGENTS.md) 和 [RESEARCH_SCOPE.md](RESEARCH_SCOPE.md)。

## 离线 CI 验收实证（2026-11-20）

GitHub Actions 运行 [44745226700](https://github.com/Justices/alpha_explore_agent/actions/runs/44745226700) 对应功能分支提交 `2654f38e82bcccd93783409909850eaa747d6c5a`，结论为 **success**。读取三个 Job 和原始离线测试日志，确认：

| 运行环境 | 编译 | 完整 unittest | 其他 |
| --- | --- | --- | --- |
| Ubuntu / Python 3.11 | PASS | **616 tests, OK** | Registry CLI、离线 Model Smoke、Mock Research CLI 通过 |
| Windows / Python 3.11 | PASS | **616 tests, OK** | Registry CLI、离线 Model Smoke、Mock Research CLI 通过 |
| Ubuntu AGY SDK 合同 | — | — | SDK 安装与只构造配置的契约检查通过 |

该执行没有向测试 Job 注入真实模型或 BRAIN 凭证。此结果属于**离线、双平台、当前提交**的确认，不证明生产 BRAIN 鉴权、真实数据可用性、平台 Simulation 的安全性或真实费用。

注意：更早的测试报告中“待运行验证”的记载是验收前的快照，不能覆盖本节新取得的实际 CI 证据。未来新增提交必须重新核对该提交的 CI 状态。

## 判定口径

- **代码证实**：已从当前分支读取相关实现，可以确认接口/状态机等静态事实。
- **用户环境已验证**：操作者提供了可核对的本地运行输出；不等于在审计环境重放。
- **待运行验证**：已经有测试代码或设计描述，但无对应的成功执行报告。
- **尚未集成**：没有把生产执行链路纳入统一控制平面，或模块明确禁用了该能力。
- **不在范围**：Alpha 自动提交、未授权的 Simulation 或属性变更。

## 审计矩阵

| 设计职责 | 代码位置 | 审计判定 | 依据与边界 |
| --- | --- | --- | --- |
| 模型预设与 Backend 校验 | `agents/registry.py`, `agents/backend_validation.py` | 代码证实 | 注册项独立验证，`select_registered_backends` 使用已注册定义；明文模型密钥被配置校验拒绝 |
| 显式角色映射与任务路由 | `agents/role_profile.json`, `agents/router.py`, `cli.py` | 代码证实，回归待运行 | Lead/Executor 根据任务绑定；显式选择现在取内置角色表，避免旧业务 `model` 覆盖 |
| 模型 Credential 安全边界 | `integrations/model_openai.py`, `agents/doctor.py` | 代码证实；DeepSeek 实际双角色探测由用户报告通过 | 真正的密钥仍只来自 `api_key_env` 对应进程环境变量，调用和 Doctor 均不写入密钥 |
| CLI / Preflight 模型就绪口径 | `cli.py`, `agents/readiness.py`, `preflight.py` | **已修复一处不一致；待回归** | 原 Preflight 会从旧业务 `model` 读取 Role Profile；已改为与 CLI 显式模型选择相同的权威角色表 |
| 模型调用账本和恢复隔离 | `integrations/model.py`, `agents/governance.py`, `orchestration/recovery.py` | 关键设计存在；故障注入待运行 | 模型身份/预算、调用证据与研究恢复相互独立；未知结果不可默许重试 |
| 统一四阶段任务编排 | `orchestration/engine.py` | 代码证实，离线测试待运行 | `validate → execute → qualify → collect`；持久检查点、执行预算和任务级互斥 |
| 离线编排适配 | `orchestration/offline_adapter.py` | 代码证实，离线测试待运行 | `OfflineResearchAdapter` 消费固定 fixtures，`MockWorkflowAdapter` 要求明确的 mock Gateway/Agent |
| 统一编排生产执行 | `orchestration/engine.py`, `orchestration/dispatch.py`, `cli.py` | **尚未集成** | `MainOrchestrator` 无适配器即拒绝执行；原有 CLI 仍可通过独立领域入口 dispatch，并不等于统一任务控制平面已连接生产 |
| 三类研究流程 | `workflows/regular`, `workflows/super_alpha`, `workflows/optimization`（由 dispatch 引用） | 接口入口存在；真实端到端待验证 | 仍须分别验证 Scope、模型决策、Intent、平台读回、恢复和资格链路 |
| Candidate / Preferred 共用资格门槛 | `qualification/service.py`, `qualification/pool.py` | 共用服务实现存在；真实审计待验证 | 先检查 RA，再进行适用相关性和其余门槛；Candidate/Preferred 各自版本化，缺失证据不能算通过 |
| 模型双角色 Smoke Test | `agents/probe.py` 和操作者报告 | **用户环境已验证** | 2026-11-20 DeepSeek 两次 `model_probe_v1` 均 PASS；分别报告 345 / 349 Tokens；`brain_invoked=false` |
| 真实 BRAIN Simulation 与 Alpha 提交 | 受限生产领域流程 | 未验证 / 不在范围 | 本轮未执行；Alpha 自动提交不受当前系统授权 |

## P1 统一编排接入进展（离线控制平面）

已确认现有 `MockWorkflowAdapter` 是复用领域工作流的正确边界：它把统一任务控制平面的 `validate / execute / qualify / collect` 映射到既有 `dispatch`，不复制 Regular、Super 或 Optimization 研究逻辑。

- `validate`：只校验最小任务输入。
- `execute`：每个统一任务只调用一次既有领域命令（Regular `run-campaign`、Super `run-super`、Optimization `run-optimization`），并传递执行上下文，使领域流程能够绑定恢复目标。
- `qualify`：提取领域工作流已经生成的资格证据，**不重复执行 CandidateQualificationService**。
- `collect`：采集资格摘要和只读 Candidate Pool 快照。

已有 `tests/test_workflow_adapter.py` 覆盖三类工作流的命令映射、单次 dispatch、资格证据检查点和预算结算；`tests/test_orchestrator.py` 覆盖通用状态机和失败阻断。本轮移除了重复新增的测试，仅保留 `tests/test_offline_model_orchestration.py` 中两个独有的模型角色路由和 Lead → Executor 持久检查点交接用例。所有这些测试都属于离线契约验证，不能代替真实领域代码与 BRAIN Gateway 的端到端验收。

### 生产 Adapter 的边界（尚未实现）

不应把 `MockWorkflowAdapter` 直接改造成可切换 Live/Mock 的通用适配器。生产 Adapter 需要单独的显式实现和审批，至少包括：
1. 复用 CLI 当前的配置装配、模型角色注册、调用预算、Gateway 身份和进程级锁，不允许自行构造第二套凭证或预算账本。
2. 对每个 workflow/step 定义领域状态与统一状态的映射，不能将“领域流程内部已完成 Simulation”错误地解释成可重复执行的 execute 阶段。
3. 领域流程在任何外部副作用之前，必须持久化并绑定准确的 Simulation Intent/批次引用；结果未知时交给现有 RecoveryCoordinator，不重新 dispatch。
4. Candidate Qualification 只允许一个权威执行方；统一控制层只消费证据，不能二次跑 RA、相关性或平台检查。
5. 对生产执行引入单独的命令授权开关、可审计预算和明确的人工确认。禁止通过 CLI 参数或配置默认值悄悄开放真实 Simulation。

在以上契约完成设计审查之前，生产 Adapter 保持未实现；本轮不开放真实执行。

## P1.2 领域恢复与状态一致性核对

本轮从 `orchestration/recovery.py`、`orchestration/engine.py`、`simulation/intent.py`、`tests/test_recovery_coordinator.py` 以及 `tests/test_workflow_adapter.py` 核对以下边界：

| 工作流 | 恢复目标 | 允许恢复的已提交证据状态 | 统一任务的后续动作 |
| --- | --- | --- | --- |
| Regular | 保存了 `simulation_intent.json` 的批次目录 | `SUBMITTING` 或 `READ_BACK`（读回需已有结果） | 经领域恢复及 Campaign 对账生成执行结果，随后由经签名/摘要核验的恢复收据完成任务 `execute` |
| Super | 受控 state root 下的 Simulation Intent 文件 | `POSTING` / `OUTCOME_UNKNOWN` / `RESPONSE_RECEIVED` / `READ_BACK` | 只恢复已有平台操作或完成下游审计，禁止再次 POST |
| Optimization | 受控 state root 下的 Simulation Intent 文件 | 同 Super | 保持源 Alpha 只读和分支证据，禁止再次 POST |

- `PREPARED` 从未被视为恢复执行许可。
- 任务必须先阻断、已经持久绑定合法领域恢复目标，且 `execute` 尚在 `RUNNING` 时才允许 `recover_task`。
- Recovery Coordinator 的目标锁、领域 journal、任务 receipt 和 `MainOrchestrator.reconcile` 的摘要核验共同防止自动重放。
- 统一任务 `budget.reserved` 在结果不明时保持不释放；仅经成功执行或验真的显式协调转为 `used`。
- Regular 的任务级恢复当前只允许一批次，且必须把恢复批次与原 Campaign 收据严格关联；多批次生产任务不能直接套用这一恢复路径。

**本轮新增修复（回归仍待运行）：**
- `recovery.py` 不再把未经信任的异常消息复制到持久失败日志，改为记录异常类型和固定原因；新增含模拟敏感信息的负例测试。
- 对新增离线测试做了去重；按工作流区分资格投影结构的断言由既有 `tests/test_workflow_adapter.py` 维护。

**剩余验收门槛：** 需要在项目 Python 环境实际运行 `tests.test_recovery_coordinator`、`tests.test_workflow_adapter`、`tests.test_orchestrator` 和新加的离线编排测试；并以进程终止/重启方式验证跨进程锁、未完成 journal、Intent 与收据持久性。未取得这些结果前，不宣称恢复机制端到端已验证。

## 已处理缺陷

**AUD-001（P0，修复已提交，回归待运行）**

症状：业务配置存在旧 `model` 字段时，`models doctor` 在显式选用注册模型后可采用权威 `role_profile.json`，但 Preflight 的 `inspect_live_model_roles()` 仍调用 `load_role_profile(config)`，可能继续加载旧路由或报错。

原因：显式角色选择有两条不同的配置解析路径。

整改：`agents/readiness.py` 改为直接使用 `load_role_profile()`；补充 `tests/test_model_registry.py::test_preflight_readiness_uses_canonical_roles_with_legacy_config` 的无网络回归用例。保留 `AGENTS.md` 的 API Key 环境变量约束，不更改任何模型密钥和用户状态。

**AUD-002（文档债务，已处理）**

`alpha_research/config.py` 文件头仍引用已经从当前分支删除的 `AGENT_DESIGN.md`，已改为指向当前 `AGENTS.md` / `RESEARCH_SCOPE.md` 契约；不改配置语义。

## 离线 CI 与发布门槛

已核对现有 `.github/workflows/agent-runtime-offline.yml`：功能分支 `push` 会在 Linux 与 Windows 上执行 `compileall`、完整 `unittest discover -s tests -v`、Registry CLI 本地检查和 Mock 研究 CLI；另有 AGY SDK 构造检查。**无需另建第二套 CI。**

本轮已把 DeepSeek / GLM / Kimi / Anthropic / OpenAI / BRAIN 的凭证环境变量全部在 CI job 中显式清空。CI 不应依赖真实密钥或授权真实平台调用。

连接器目前只能返回部分触发类别的 Workflow Run 记录；因此`workflow_runs=[]` **不能证明 push CI 没有运行**，更不能证明测试通过。提交前必须在 GitHub Actions 中取得具体 Run ID、Linux/Windows 两套执行结论和失败测试日志；并核对是否存在无法复现的环境差异。

本分支相对 `main` 的变更覆盖多模块和大量历史提交。**不以本轮静态审计代替完整 Review**，也不直接合并。

## 下一轮整改与验收计划

| 优先级 | 任务 | 通过标准 | 安全限制 |
| --- | --- | --- | --- |
| P0 | Windows 运行模型注册、Doctor、Preflight 和离线编排回归 | 所有指定 `unittest` 完成并有保存的报告；失败项给出复现和修复 | 不使用真实模型密钥做单测 |
| P0 | 审核测试数据是否真正覆盖状态传递，而非仅伪造完成 | 每种工作流检查点可追溯；Lead → Executor 交接测试仅作为隔离组件验证，不能代替真实领域入口测试 | 不调用 BRAIN |
| P1 | 故障注入：执行阶段不确定结果、预算预留、跨进程锁、恢复收据 | 未确认结果不自动 POST、不自动重试；恢复必须有可验证收据 | Mock Gateway |
| P1 | 统一控制平面和实际领域入口的集成设计 | 明确生产 Adapter、鉴权与授权边界、状态转换、恢复协调、任务预算归属 | 先设计审查，后实现；不得自动启动 Simulation |
| P2 | BRAIN 只读预检 | 账户、数据集和有效配置可经当前平台证据核实 | 需要独立授权，严禁 POST |
| P3 | 受控模拟与人工验收 | 精确授权的请求数量、Intent、读回、审计及故障恢复闭环 | 单独授权；不提交 Alpha |

## 明确不应删除或提前合并的设计

- 现行根契约及批准范围（`AGENTS.md`, `RESEARCH_SCOPE.md`）；
- 模型调用审计与 Simulation Intent 两套独立恢复账本；
- Session 索引、证据完整性和跨进程锁约束；
- Candidate 与 Preferred 两级评价及历史审计；
- 已经有真实能力但尚未取得端到端验收证据的组件。

所有未执行的测试维持“待运行”状态，禁止将代码审阅或模型 Smoke 的 PASS 改写为完整系统验收 PASS。
