# Task 执行链与证据追踪

本功能沿用现有 V1.1 Task、Campaign、Research、Model Journal、Simulation Intent、Recovery 和 Candidate 记录，不建立第二套业务状态机。

## 1. 证据层次

| 证据 | 现有/新增位置 | 责任 |
|---|---|---|
| Task 状态与四阶段 Checkpoint | state/research_v1_1/orchestration/<task_id>.json | MainOrchestrator |
| Task 事件链 | state/logs/execution_events/<task_id>.jsonl | 新增 ExecutionEventJournal |
| 模型输入/输出与 Attempt 审计 | state/logs/model_calls/*.record.json、state/logs/model_log.jsonl | ModelCallJournal |
| 模型上下文/响应快照 | state/logs/model_calls/*.context.json、*.response.json | ModelCallJournal |
| 模型 Attempt / Token 预算账本 | state/logs/model_budget/<research-hash>.json | ModelCallLedger（sealed JSON） |
| 跨批次模型 Session 索引 | state/logs/model_sessions/<session_id>.json | DecisionRecorder / ModelCallJournal |
| Regular Campaign 与 Dataset 子任务 | state/research_v1_1/campaign_runs/、对应 batch 目录 | Regular Campaign |
| Super 运行记录 | state/research_v1_1/super_runs/ | Super Workflow |
| Optimization 源快照、分支、报告 | state/research_v1_1/optimization/ 与 RecordStore | Optimization Workflow |
| Simulation 生命周期 | 对应 simulation_intent.json、response、readback | Simulation Intent / domain workflow |
| 审核与优选 | state/research_v1_1/candidate/、preferred/ | CandidateQualificationService |
| 中断与恢复 | state/research_v1_1/recoveries/ | RecoveryCoordinator |

Task 事件链只保存执行身份、阶段、状态、业务 ID、证据引用和摘要 Hash。追踪命令另外生成只读 evidence_manifest，对已关联的 Task、模型上下文/响应快照、Campaign、Simulation Intent、Readback、Audit、Candidate 和批次文件计算 SHA-256。不记录 API Key、Cookie、模型 Prompt、原始模型响应或 Alpha 表达式正文。模型原始输入/输出仍由现有 Model Journal 按原有访问边界保存。

## 2. Task 事件链

每个 Task 独立写入一个 JSONL 文件，包含：
- 单调递增的 sequence；
- event_id、UTC occurred_at；
- task_id、workflow、Research ID、step；
- event_type 与结构化 payload；
- 前一事件 Hash previous_event_sha256；
- 当前事件 Hash event_sha256。

事件采用规范化 JSON 和 SHA-256 链式校验。写入使用单写者文件锁，并在追加后 flush/fsync。发现 JSON 损坏、序号断裂、前序 Hash 不匹配或当前 Hash 不匹配时，读取和追加均 fail closed，不静默修复、不覆盖历史。

当前事件覆盖：
- Task 创建、阶段状态迁移、阶段开始/完成/阻断、Task 完成；
- Domain Research ID、Recovery Target 绑定、人工协调结果；
- Regular Campaign 排程、Dataset 批次开始/完成/失败、Dataset Review、Campaign 完成；
- Super/Optimization 的提案、校验、分支、Simulation Intent、POST、Readback、源完整性和 Qualification 里程碑；
- Domain dispatch 的开始和完成。

模型调用仍由现有 Model Journal 记录；执行链通过 Research ID、Session ID、Batch Ref 和模型调用记录关联，不复制 Prompt/Response 内容。一个 Regular Campaign Task 可以包含多个 Dataset 子任务以及独立 Optimization Research ID；事件的 Research ID 按领域运行标识关联，Task 的主 Research Identity 保持兼容，追踪命令会汇总该 Task 事件链中出现的全部 Research ID 与各自的 Model Budget Ledger。

## 3. 只读追踪命令

在项目根目录运行：

~~~powershell
$Python = "D:\quant-venv\Scripts\python.exe"
& $Python -m alpha_research --state-root "D:\quant\alpha_explore_agent\.alpha-runtime" orchestration-execution-trace <TASK_ID>
~~~

该命令：
1. 读取 Task；
2. 完整验证该 Task 的事件 Hash 链；
3. 依据事件链中的全部 Research ID 关联 Model Call Records、Session Index 和 sealed Model Budget Ledger（Attempt 状态及 Token 结算），包括 Regular Campaign 内部派生的 Optimization 子流程；
4. 读取对应 Campaign / Super / Optimization 记录；
5. 解析已引用 Simulation Intent 的状态和响应/Readback 引用；
6. 输出结构化 JSON，并提供 evidence_manifest（文件路径、大小、SHA-256；批次目录仅列文件元数据，不输出内容），不调用模型、不访问 BRAIN、不修改研究状态。

成功输出 status=TRACE_VERIFIED 和 journal_verification.status=VERIFIED。事件文件缺失或损坏时，不会重建历史，也不会把缺失的事件当作验证成功。

## 4. 复盘与排障顺序

1. 先确认 Task 的 workflow、status、四阶段状态、预算和 Research ID。
2. 验证事件链；事件链不完整时先保全原文件并检查备份，不要编辑 JSONL。
3. 依据最后一个事件定位阶段，再查看该事件引用的 Campaign、Simulation Intent 或 Recovery Target。
4. 用 Research ID 查看 Model Call Records；用 Session ID 查看跨批次索引；对照 context/response Hash 和 manifest。
5. 以 Simulation Intent、平台 Readback 和 Recovery Receipt 判断实际外部结果；不能仅凭本地 Task 状态推断远端 Simulation 是否成功。
6. 以 Candidate / Preferred 记录及 evidence_refs 审核候选。RA fail count 未归零时不得进入相关性优选；优选阈值仍按既定 PC/SC 规则执行。

## 5. 日志保留策略

- 一个 Task 的 JSONL 事件链与其 Task 文件必须作为同一证据单元备份。
- Model Call Records、context/response 快照、Session Index、Batch Manifest 不得单独清理；保留其 Hash 与相互引用关系。
- Campaign、Simulation Intent、Readback、Audit、Candidate、Preferred、Recovery Receipt 及其源快照按引用闭包保留。
- 生产日志轮转/压缩只能在独立归档副本中进行；活动 JSONL 不得就地截断。归档应保存原始文件 SHA-256、文件大小、时间范围、Task ID 和备份批次。
- 任何 UNKNOWN、BLOCKED、OUTCOME_UNKNOWN、缺少 Readback 或 Hash 不一致的任务必须保留完整现场，不得自动重试、删除或覆盖。
- 日志保留周期、加密、备份位置、访问控制和异地灾备仍需结合本地/腾讯云部署环境制定；本次代码不会擅自配置或上传研究证据。

## 6. 证据完整性边界

SHA-256 Hash 链可以检测意外修改和无密钥篡改，但不能证明本地管理员无法同时重写整个日志链，也不能证明平台返回内容真实。需要更高等级的不可抵赖性时，应将已封存的 Task 证据包定期复制到独立权限控制的对象存储/WORM 介质，并由独立审计身份签名；该外部保全能力不在当前本地日志实现的承诺范围内。
