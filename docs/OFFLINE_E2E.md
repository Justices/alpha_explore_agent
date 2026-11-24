# 离线全流程验收：一次命令，独立 State Root

本入口验证 **Task 创建、提案与角色身份绑定、编排四阶段、域调度、候选投影、Checkpoint、恢复目标及失败后的禁止重试**。它不等价于真实 WorldQuant BRAIN Alpha 研究验收。

## 适用范围与安全边界

- `orchestration-e2e-offline` 会调用真实的 `MainOrchestrator`、`dispatch` 和 Regular Campaign 调度器。
- **模型生成、Regular 单批次研发执行以及 BRAIN 网关均为脚本化模拟**。Super/Optimization 的 Simulation 也是脚本化模拟。因此不能将任何候选指标或 Alpha ID 视为真实平台结果。
- 不初始化 `Gateway`、不访问 BRAIN、不调用收费模型、不真实 Simulation、不提交 Alpha。
- 要求**全新、空目录**作为独立 `--state-root`，绝不能使用有真实数据的 `.alpha-runtime` 或 `alpha-research-live`。
- 运行后目录被永久标记 `OFFLINE_DOMAIN_MOCK_ONLY`，不得复用于真实任务。每次重新验收换一个新目录。
- 配置来自系统默认值，指定 Dataset/Category/Region/Delay/Universe 等 Scope 条件在生效前进行冲突检查；配置文件中固定政策不能覆盖系统默认值。

## Regular（USA / Delay 1 / TOP3000 / analyst10）

Windows PowerShell：

```powershell
$Python = "D:\quant-venv\Scripts\python.exe"
$E2ERoot = "D:\quant\alpha_explore_agent\.alpha-offline-e2e-01"

& $Python -m alpha_research `
  --state-root $E2ERoot `
  --lead deepseek --executor deepseek `
  orchestration-e2e-offline `
  --mode mock --workflow regular `
  --objective "Regular single-dataset offline acceptance" `
  --region USA --delay 1 --universe TOP3000 `
  --dataset analyst10
```

成功返回 `status=OFFLINE_E2E_PASSED`，`task_status=COMPLETED`，四个步骤均 `DONE`，Task 预算为 `limit=1,used=1,reserved=0`。返回的 `task_id` 对应永久保存的 Task 文件；`report_path` 是单次验收报告。

成功只代表**离线编排及域接口验证**，并非真实研究有效、真实 Model/Simulation 已运行或已经获准生产发布。

## 其他工作流

Super 使用新的空目录并指定 `--workflow super`。Optimization 使用新的空目录、`--workflow optimization --alpha-id SOURCE`，其中 `SOURCE` 为脚本化源 Alpha，仅供离线调度验证。

## 失败与故障演练

添加 `--fault lost-simulation-response` 可以模拟结果丢失。预期 `status=OFFLINE_E2E_BLOCKED`，`budget.reserved=1`，`budget.used=0`，并保留恢复目标以供调查。不能自动重放，也不能使用此目录再次运行新任务。

## 后续：真实受控 Regular Pilot（不在此命令中自动运行）

1. 在**真实、未标记 mock 的** State Root 执行 `preflight`，检查模型配置、本地凭证和固定政策。
2. 使用已有 `orchestration-prepare --workflow regular --region USA --delay 1 --universe TOP3000 --dataset analyst10` 生成一个全新 Task，确认 Task/Scope 与单批次预算。
3. 原有 `orchestration-local-pilot-regular` 仅在用户明确授权一次模型调用与一次 BRAIN Simulation、并提供 `--allow-model-call --allow-brain-simulation` 两项标志后才能启动。这可能产生模型费用和真实平台写入；**不能把离线验收成功当作授权**。
4. 实际完成后再审查 Alpha 指标及 RA 失败数；只有 RA fail count = 0 时，才进入 PC/SC 相关性判断，严格要求各项相关性 < 0.7。任何 BRAIN 超时或未知结果均需人工核对原始 Intent，不自动重试。

不要用 `orchestration-e2e-offline` 生成的 Task ID 进入真实 Pilot；两个 State Root 和运行模式严格隔离。
