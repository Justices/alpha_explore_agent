# Alpha Research 运维手册

> 本手册覆盖当前代码的**操作边界**。离线诊断和本地容器演练不是生产部署。真实模型付费、BRAIN Simulation、属性修改和 Alpha 提交必须由对应的独立授权契约处理；当前新 Research 控制平面仍为 `P7_PRODUCTION_BLOCKED`。

## 1. 按 Task 检查研究状态（只读）

使用与 Task 创建时相同的 `--state-root`、角色配置和工作分支：

```bash
python3 -m alpha_research \
  --lead deepseek --executor deepseek \
  orchestration-research-status 6095655c13d44733acbae021f1afd88e

python3 -m alpha_research \
  --lead deepseek --executor deepseek \
  orchestration-release-gate 6095655c13d44733acbae021f1afd88e
```

第一条是 Task/Model/Manifest/Catalog/Recovery 的只读状态汇总；第二条只给出生产阻断矩阵，不签发执行 Grant。它们不要求自行手写 Manifest，也不应触发 BRAIN 或模型收费调用。任务不存在、身份冲突、损坏的 Hash、模型 Attempt UNKNOWN、缺失收据应按报告分类，不重建并覆盖旧记录。

## 2. 隔离离线研究演练

**仅使用新的、一次性 state root**，不能指向真实 BRAIN 证据目录或已运行任务：

```bash
python3 -m alpha_research \
  --state-root /tmp/alpha-research-sandbox \
  --lead deepseek --executor deepseek \
  orchestration-prepare --workflow super \
  --objective "offline Super rehearsal" \
  --region USA --delay 1 --universe TOP3000

python3 -m alpha_research \
  --state-root /tmp/alpha-research-sandbox \
  --lead deepseek --executor deepseek \
  orchestration-research-prepare <NEW_TEST_TASK_ID> --mode mock
```

命令中的 `<NEW_TEST_TASK_ID>` 必须由上一条结果替换；对未知执行结果不得重试或重建同一 Task。模拟数据和 SDK MockTransport 不能用于证明真实 Provider 发票、真实 BRAIN Session 或可提交 Alpha。

## 3. 本地 Authority 参考服务

- 实现：`alpha_research/orchestration/research_authority_server.py`、`research_authority_client.py`、`research_authority_deployment.py`，配置/镜像在 `deploy/p7/`。
- 参考服务使用 SQLite 事务维护一次性授权消费、USD 微单位预算保留、Audit/Recovery 和 Global Stop。API token 与 admin token 必须不同；只允许管理受控的参考服务。
- Linux Compose 使用 `network_mode: host` 与服务端 **`127.0.0.1:8765`**，不发布公网端口，不可机械移植到 Windows/macOS 主机网络。
- 容器以 UID 65532 非 root 运行，根文件系统只读；私钥/证书及 token 文件由独立操作者配置，文件通常为 UID 65532 所有、mode 0600，受控目录通常为 mode 0700。不要把密钥、证书、真实 BRAIN Cookie 或 token 提交到仓库。
- `deploy/p7/config.template.json` 仅是字段模板，不是可运行授权配置。真实 CA、服务器证书、Client CA、API/admin token、Grant/Ledger signer 及预算上限需分别托管并核对。
- 应用只负责本地只读健康检查及离线演练；独立的生产 PKI、网络身份、密钥轮换、审计保全、备份恢复和 on-call 尚未证明。

仅在独立、已准备好的 Linux 演练机器上由操作者选择执行：

```bash
docker compose -f deploy/p7/compose.yaml config
docker compose -f deploy/p7/compose.yaml build
docker compose -f deploy/p7/compose.yaml up -d
docker compose -f deploy/p7/compose.yaml ps
```

这些是**运维参考命令**，并非本轮聊天已执行的部署。不得把 host-loopback 改为 `0.0.0.0`、添加公网端口或用反向代理绕过已核对的 mTLS/身份边界。数据库要采用一致性的 SQLite backup（包括 WAL），不能停机后自动清除 STOPPED 记录。

## 4. 费用、账单和不可重试操作

- 按 Attempt/Provider Operation ID 验证唯一性；保留金额、预估 Tokens/费用和用户给的 invoice 行需要完整匹配；使用整数 micro-USD。
- **`P7_INVOICE_MATCHED_UNTRUSTED_INPUT` 不是 Provider 官方结算确认**；需要独立来源、真实价格版本、硬预算及授权的状态协调流程。
- 模型请求已派发但响应未知、BRAIN Simulation 可能已 POST、Stop 信号与费用账本冲突时，保留 UNKNOWN/阻断，不自动取消预算、退款或重试。
- `research_p7_canary.py` 只产生 Stop 建议；真正的 admin Stop 是不可逆的外部操作，必须由获得批准的操作者基于可信通道执行。

## 5. 事件响应与生产部署缺口

| 情况 | 安全处理 |
| --- | --- |
| UNKNOWN Provider 或丢失 ACK | 通过真实 Provider operation readback 协调，预算不释放、不自动重试 |
| Session、Scope 或 BRAIN 帧被拒绝 | 停止该研究通道；确认已签名身份与独立只读权限，不通过改 JSON 绕过 |
| GLOBAL STOP 为 STOPPED | 不自动重置；检查持久 Stop Event、审计账本及业务进程实际停止情况 |
| Canary 心跳过期、账单差额或 Counter 回退 | 保持阻断并要求人工处理；用可信远程证据复核 |
| 本地 CI 全绿 | 只说明离线契约通过；继续取得独立部署、账单、真实 BRAIN Session、真实 Canary 和 Simulation 分别授权 |

发布前验收责任、代码和外部边界详见 [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md)，系统功能与模块 ownership 详见 [CURRENT_DESIGN.md](../design/CURRENT_DESIGN.md)。

## 本地 CLI 的职责和安全边界

`cli_parser.py` 只定义参数；`cli_orchestration.py` 负责研究准备/授权审计/模拟演练命令，沿用离线封闭和鉴权拒绝语义；`cli_status.py` 只聚合 state-root 内已有记录。`cli.py` 保留实时 BRAIN/模型命令的各自显式安全条件；CLI 拆分不意味着这些入口获得新的生产执行许可。

## 本地真实 Dataset 目录诊断（只读）

`brain-readonly-check` 只验证单页 Catalog 可读，返回的 `dataset_count` 不是完整分页下载的证据。`plan-regular --dataset <ID>` 需要先下载和校验完整的 exact-scope Dataset Catalog，然后才查询给定 ID 的详情。若返回 `dataset_catalog_unavailable`，新版本的 `unresolved[].catalog_error_code` 提供固定、无敏感内容的错误分类（HTTP 拒绝、分页计数变化、重复 ID、响应格式、本地存储或未知）。这不会改变 fail-closed 行为；不要用单页成功代替全量 Catalog 完整性，也不要绕过缺失的 Scope 证据直接 Simulation。`CATALOG_UNKNOWN` 或没有分类码时应检查隔离的受控日志和当前 SHA；不要将真实原始凭证、响应头或错误载荷贴到公共渠道。
