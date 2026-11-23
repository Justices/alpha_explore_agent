# Alpha Research — 组件与依赖架构

本文件是 [整体设计](CURRENT_DESIGN.md) 的**模块级详细补充**。业务决策以 [已批准范围](RESEARCH_SCOPE.md) 为准，研究安全以 [AGENTS.md](../AGENTS.md) 为准。本文不授予执行权限。

## 业务边界

| 业务 / 共享能力 | 入口与依赖 | 不变量 |
| --- | --- | --- |
| Regular | `workflows/regular/` | Campaign → Dataset → Combination → Batch，Delay 1、规则覆盖和完整模拟意图 |
| Super | `workflows/super_alpha/workflow.py` | 使用 BRAIN-native 选择/组合机制；不套用 Regular Dataset 规则 |
| Optimization | `workflows/optimization/workflow.py` | 源 Alpha 设置只读，候选分支独立研究和审计 |
| Qualification | `qualification/` | RA/相关性/指标先于 Candidate；Preferred 只能消费 Candidate |
| Research context | `knowledge.py`, `expressions/` | 规则、字段、操作符、平台证据与模型推理分离 |

## 技术边界

```text
cli.py / workbench
  -> agents (Registry + Role Profile)
  -> orchestration (Task, checkpoints, release/audit, recovery)
  -> workflows (domain ownership)
      -> integrations/brain (Gateway / Driver)
      -> integrations/model* (CLI / SDK / LLM API)
      -> expressions + simulation + qualification
  -> state (file storage, locks, attempts, evidence)
  -> knowledge (Dataset/field/operator snapshots)
```

依赖方向以业务功能为中心：模型和 BRAIN 都是可替换的外围适配器；同一个任务不得创建额外的第二份预算或审批权威。CLI 只能装配角色与解析输入；领域流程拥有研究设置和候选决策。平台响应是事实来源，模型文本是建议，签名审计是技术观察，独立部署的 Grant/预算/Stop 才可能构成后续生产授权链的一部分。

### Orchestration 控制平面

- `engine.py`: `validate → execute → qualify → collect`、任务身份、预算检查点和重入；
- `workflow_contract.py`、`dispatch.py`: 领域命令映射与适配器注入；
- `research_preparation.py`、`research_runtime_adapter.py`: BRAIN/Model 前的离线证据准备，不可当成真实执行权；
- `research_authority_*.py`、`research_grant_service_reference.py`、`research_spend_service_reference.py`: 独立 Reference Authority、单次消费、签名审计、费用保留和恢复；
- `research_p7_billing.py`、`research_p7_brain_read.py`、`research_p7_canary.py`: 离线账单、BRAIN Frame 与 Canary 观察产生者；`release_contracts.py`、`release_evidence.py`、`release_matrix.py`: 统一状态词表、严格证据校验和跨模块发布矩阵；旧 P7 release 模块仅保留兼容导出；
- `production_adapter.py`: 仍是不可执行的 fail-closed 接口，不是已授权的真实 BRAIN Adapter。

### 状态隔离与恢复

Task/Model Attempt/BRAIN Intent/费用保留/授权消费/资格审计各自有唯一 ID、状态和恢复语义。模型输出或本地 Mock 完成，不得推进尚未完成的 BRAIN 操作；结果未知先进入协调状态而不是调用下一次 `POST`。本地文件摘要只检测变更，不能替代数字签名或远程读回。详细的数据模型见 [RESEARCH_STATE_V1_1.md](RESEARCH_STATE_V1_1.md) 和 [RESEARCH_REFACTOR_BOUNDARIES.md](RESEARCH_REFACTOR_BOUNDARIES.md)。

## 执行与权限边界

现有新 Research 控制平面只提供本地离线准备/审计及负向生产状态。真实 SDK 调用、BRAIN Session 和 Simulation 与自动提交不能因组合模块而被开启。**`P7_PRODUCTION_BLOCKED`** 是现行发布结果。参考 [整体设计](CURRENT_DESIGN.md#5-权限密钥brain-只读与生产发布边界)、[运行手册](../docs/OPERATIONS.md)、[发布验收](../docs/RELEASE_ACCEPTANCE.md)。

## 维护准则

架构文档依照**责任、依赖方向、数据与信任边界**组织；不依历史开发顺序列功能清单。若新增新的模块或操作，先确认领域所有权、状态机、平台副作用、预算归属和操作授权，再更新本文与整体设计。
