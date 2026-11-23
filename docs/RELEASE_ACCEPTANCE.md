# 统一验收与发布安全矩阵

> 验收对象是**具体提交 SHA、具体执行路径、具体独立证据**，不是某个开发阶段编号。所有离线 CI 和模拟服务成功不等于已经可以使用 Provider 付费、BRAIN Simulation 或 Alpha 提交。

## 一、验收维度

| 功能域 | 静态 / 离线可验证的主张 | 需要独立运行证明的主张 |
| --- | --- | --- |
| 角色注册和模型 SDK | Registry/Role 配置校验，拒绝混用 Provider，SDK MockTransport 断网、零隐式重试 | 真正 Provider token 账本/价格版本/正式开票读取及硬额度 |
| 领域研究工作流 | Regular/Super/Optimization 的 Scope、表达式、候选、PPA、失败恢复与 Mock | 已授权且任务绑定的 BRAIN Session/仿真、平台读回与真实 Candidate |
| Grant 和 Authority | mTLS 参考客户端、SQLite 原子一次性消费、费用保留、Stop、重启持久性 | 独立生产 PKI、部署主机、签名密钥、ACL、审计/容灾与运维 |
| BRAIN Catalog read | Session/ACK 证据审计、1–64 帧约束、精确只读 Scope、摘要血缘 | 可撤销、独立认证的真实平台只读会话与收发链完整性 |
| Provider Billing | 完整 attempt/operation 集合、整数 micro-USD、差异与保留金额限制 | 经 Provider 独立认证的 invoice、operation、Token、tariff |
| Canary / Stop | 新鲜度、异常计数、持久 Stop、不回退、负向告警判定 | 操作者监督的 live Canary、可信告警，Stop 传至各真实 Provider/BRAIN 执行端点 |
| Release gate | Task/Scope/证据哈希一致性、伪造权限拒绝、全部授权输出 false | 独立审查和授权；真实 Simulation POST 仍需单独批准，自动 Alpha 提交禁止 |

## 二、离线验证命令（仓库根目录）

```bash
python3 -m compileall -q alpha_research tests
python3 -m unittest tests.test_p7_contracts -v
python3 -m unittest tests.test_p7_billing -v
python3 -m unittest tests.test_p7_integrated_release -v
python3 -m unittest discover -s tests -v
```

执行前必须核对隔离 runner 没有真实 BRAIN/Provider 凭证，不要把默认用户数据 state-root 当作测试目录。Linux Docker 的 `deploy/p7/container_smoke.py` 是一次性 mTLS + 持久 Stop 演练，**不是生产容器部署**。还需在 Windows/macOS/Linux 校验 Python 依赖、进程清理、文件锁、路径与跨进程事务。

## 三、可核查历史 CI 基线与当前状态

截至 Git SHA `e0f76f94edd209b59ea1123d23d6bf957aa32ec7`：
- [运行时离线验证](https://github.com/Justices/alpha_explore_agent/actions/runs/45188802861)：5/5 Jobs 成功，含 Ubuntu、Windows、macOS（Python 3.11/3.13）、AGY SDK；Ubuntu 全量 **959 tests, OK**。
- [隔离 Authority/发布门禁验证](https://github.com/Justices/alpha_explore_agent/actions/runs/45188802903)：3/3 Jobs 成功，含三平台及 Linux 一次性 Docker 验收。

这些证据严格绑定上述 SHA。后续更改代码/测试/文档必须针对更新后的 SHA 查看对应 CI；文档新增或老 CI 成功不能覆盖新提交的失败。

## 四、不可由本地审计消除的生产阻断项

1. **独立 Authority 和托管身份**：独立部署主机、CA/签名密钥、服务证书、Grant/Ledger 消费者权限、备份/不可逆 Stop 及生产运维责任；
2. **Provider 真实账单**：官方可认证 operation ID、使用 Tokens、Tariff、发票、信用/预算硬上限、失败 readback 与独立结算；
3. **BRAIN 真实只读权限**：独立会话认证、Task/Scope/命令绑定、可撤销性和真实请求响应读回；
4. **Canary 监督**：操作者明确批准、可信远程监控/报警、执行端点 Stop 传播及事故处理；
5. **非幂等生产操作单独审批**：Simulation 的 POST、费用、请求数量和恢复对象逐一批准；自动提交 Alpha 始终不受支持。

当前新 Research 控制面不可将任何本地 CI、签名观察、发票样例或 ARMED 状态提升为正向权限。正式 Release 结果是 `P7_PRODUCTION_BLOCKED`，`permission_to_dispatch=false`、`model_call_authorized=false`、`brain_read_authorized=false`、`brain_simulation_authorized=false`、`alpha_submission_authorized=false`、`automatic_retry_allowed=false`。

## 五、变更管理

每次改造按**模块/故障模式**组织：修改代码与对应负例 → 验证静态契约 → 全量离线回归及跨平台 CI → 对比 Git diff → 审核真实外部缺口。记录 Head SHA、CI run、测试数及异常分类，不以历史开发阶段的完成状态代替当前能力。相关日常操作见 [OPERATIONS.md](OPERATIONS.md)。

## 对外只读发布报告的负向权限契约

顶层 `inspect_production_release_gate` 和各研究子模块都显式输出 `permission_to_dispatch=false`、`brain_read_authorized=false`、`model_call_authorized=false`、`brain_simulation_authorized=false`、`alpha_submission_authorized=false`、`automatic_retry_allowed=false`；不能依赖“字段缺失代表 false”的消费方默认值。CLI/状态报告继续使用同一负向约定。Canary 心跳保留 `monitor_age_microseconds` 与 `heartbeat_limit_seconds`，从而可验证阈值边缘的状态声明。此契约只验证审计报告一致性，不是独立第三方签名。

统一离线验收也覆盖领域适配器之间的导入契约：Provider 审计、BRAIN Frame 策略、发布阻断矩阵、SDK Mock 演练均为独立模块；旧的历史名称只是兼容导出，不产生第二套状态或授予任何新权限。
