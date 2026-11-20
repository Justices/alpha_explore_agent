# 设计—代码一致性审计（2026-11-20）

> 审计范围：`refactor/configurable-codex-agy-runtime` 功能分支的关键代码与现行设计文档；**静态代码审计**，不是自动化测试通过声明，也不是 BRAIN 生产验收。遵循 [AGENTS.md](../AGENTS.md) 和 [RESEARCH_SCOPE.md](RESEARCH_SCOPE.md)。

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

## 已处理缺陷

**AUD-001（P0，修复已提交，回归待运行）**

症状：业务配置存在旧 `model` 字段时，`models doctor` 在显式选用注册模型后可采用权威 `role_profile.json`，但 Preflight 的 `inspect_live_model_roles()` 仍调用 `load_role_profile(config)`，可能继续加载旧路由或报错。

原因：显式角色选择有两条不同的配置解析路径。

整改：`agents/readiness.py` 改为直接使用 `load_role_profile()`；补充 `tests/test_model_registry.py::test_preflight_readiness_uses_canonical_roles_with_legacy_config` 的无网络回归用例。保留 `AGENTS.md` 的 API Key 环境变量约束，不更改任何模型密钥和用户状态。

**AUD-002（文档债务，已处理）**

`alpha_research/config.py` 文件头仍引用已经从当前分支删除的 `AGENT_DESIGN.md`，已改为指向当前 `AGENTS.md` / `RESEARCH_SCOPE.md` 契约；不改配置语义。

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
