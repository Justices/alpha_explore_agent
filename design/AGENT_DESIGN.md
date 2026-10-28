# nastu_quant — Alpha 探索 Agent 原始设计文档

> **当前口径（2026-10-28）**：本文保留早期 Muse harness 与组件化设计，
> 只作为参考输入。实际研究规则以仓库根目录 `AGENTS.md` 为准，运行时入口为
> `python -m nastu_quant`，BigModel 等模型服务通过 JSON 配置选择。BRAIN 目录、
> 字段、算子、模拟、恢复和最终审计接入 `BRAIN_FLOW_HOME/scripts`；必要只读
> 回退经 `brain_session.py`。本项目不维护第二份 BRAIN 登录状态，也不直接
> 调用平台模拟创建接口。旧固定 scope、cookie/AuthManager、Alpha 属性标记、
> YAML 配置、Reporter/TrafficWatch 等条目不代表当前行为；当前实现范围以
> `AGENTS.md` 和 `README.md` 为准。
> 下文固定 Scope、严格 `<0.70` 的单项相关阈值、YAML 配置、
> `set_alpha_properties` 白名单、Reporter/TrafficWatch 等描述属于旧设计；
> 当前实现范围与未完成项以 `README.md` 为准。当前固定 Dataset 试运行
> 为 GLB / D1 / TOPDIV3000 / analyst69。

| 项 | 内容 |
|---|---|
| 产品名称 | nastu_quant |
| 文档版本 | v1.0（已定稿，2026-10-23） |
| 依据 | 《nastu_quant 需求规格说明书》v1.0（AGENT_REQUIREMENTS.md，已定稿）——本文每项设计均可追溯至该文件条目 |
| 流程位置 | 软件开发流程第 2 步：设计。本文件定稿后才进入代码实现 |
| 参考输入 | AGENT_WORKFLOW_DESIGN.md（早期设计稿）、brain_driver.py、alpha_validator.py——均为参考，不构成设计基线；与本文冲突时以本文为准 |

---

## 1. 设计目标与原则

### 1.1 设计目标

把需求规格说明书中的研究流程、门禁与纪律，转化为一套可直接
指导实现的系统设计：组件划分明确、状态显性持久、关键规则由
结构而非自觉保证。本文覆盖组件设计、状态机、数据与文件设计、
关键机制、接口、配置、异常处理与需求追踪。

### 1.2 设计原则

1. **门禁即结构**：凡需求中「不得」「不许」的规则，优先用
   状态机与接口层校验实现为「不可能」，其次才用流程约定
   （对应需求 A2 的结构性验收口径）。
2. **状态落盘、进程无状态**：一切研究状态存于文件，进程可
   随时被杀并从产物恢复（NFR-01）。进程内存不持有唯一状态。
3. **单主控串行**：研究主链由单一主控按批次顺序推进
   （FR-08），不用多进程并行换取速度；并行只允许出现在
   无副作用的侧任务（如资料查阅），且不进入主链状态。
4. **复用已验证基线**：平台通信复用 brain_driver.py 的
   JSON-lines 协议与命令集，表达式验证复用
   alpha_validator.py 的解析器与规则引擎，二者按 §9 的
   改造清单适配新需求，而不是重写。
5. **每个结论有产物**：判定、归因、计量全部落盘为结构化
   文件，汇报由产物生成，不由记忆生成（NFR-02）。

## 2. 系统架构

### 2.1 组件总览

系统由一个主控与十二个职责单一的组件构成，全部运行在
Muse harness 之上，平台访问统一收口于 Gateway 组件。

```
                        ┌────────────┐
            用户 ◄────► │  Reporter  │ 播报 / 汇报 / 审批交互
                        └─────┬──────┘
                              │
                        ┌─────▼──────┐
                        │Orchestrator│ 主控状态机（主链调度）
                        └──┬───┬───┬─┘
             ┌─────────────┘   │   └─────────────┐
             ▼                 ▼                 ▼
      ┌────────────┐   ┌────────────┐   ┌────────────┐
      │ Admission  │   │  Executor  │   │ Gatekeeper │  …
      │ 准入/探针   │   │ 执行/找回   │   │ RA 门禁     │
      └─────┬──────┘   └─────┬──────┘   └─────┬──────┘
            │                │                │
            ▼                ▼                ▼
      ┌────────────────────────────────────────────┐
      │        Gateway（driver 进程客户端）          │ ← 唯一平台通道
      └────────────────────┬───────────────────────┘
                           ▼
                    BRAIN 平台（cnhkmcp）
      ┌────────────────────────────────────────────┐
      │  StateStore：状态 / 台账 / 先验 / 计量（文件） │ ← 全部组件共用
      └────────────────────────────────────────────┘
```

### 2.2 组件清单

| 编号 | 组件 | 职责 | 对应需求 |
|---|---|---|---|
| C1 | Orchestrator | 主控：驱动研究主链状态机，调度各组件，执行授权模式与急停 | FR-08、FR-12 |
| C2 | AuthManager | 会话两级恢复（cookie → 凭证文件）、needs_auth 挂起 | FR-01、NFR-03 |
| C3 | Gateway | driver 进程的唯一客户端：命令白名单校验、会话失效感知、相关性检查的状态前置校验 | FR-02、IF-01、FR-10 |
| C4 | Admission | 数据集准入：live 核验、字段目录核对、准入结论落盘 | FR-03 |
| C5 | ProbeRunner | 六项探针的编排与执行、探针报告与约束翻译的生成 | FR-04 |
| C6 | ResearchDesigner | 假设管理与预登记：假设四要素校验、预登记锁定与作废 | FR-05、FR-06 |
| C7 | Validator | 表达式静态验证（复用 alpha_validator.py） | FR-07 |
| C8 | Executor | 批次提交（multisim 优先）、找回协议、单条回退、执行台账 | FR-08 |
| C9 | Attribution | 指标结构化提取、四分法分类、假设回判 | FR-09 |
| C10 | Gatekeeper | RA Gate 与相关性的顺序判定、Candidate 授予 | FR-10 |
| C11 | MemoryStore | 先验库、禁止重跑清单、研究台账的读写 | FR-11 |
| C12 | Reporter | 进度播报、结果汇报（先失败项）、审批请求与信号识别 | FR-13、IF-02 |
| C13 | TrafficWatch | 账户产物盘点、本系统/外部流量区分、陌生流量报告 | FR-14 |
| C14 | Metering | 批次时长、模拟数、token（可得时）的采集与汇总 | FR-15 |

StateStore 不是独立进程，而是全体组件共用的文件层规范，
见 §5。

### 2.3 运行形态

- 主控与组件以 Muse harness 内的 agent 逻辑实现；Gateway
  之下是一个常驻的 driver 子进程（沿用现行实现），主控
  通过其标准输入/输出逐行收发 JSON。
- driver 进程被杀或环境重启时，由 AuthManager 按 §6.1
  流程重建进程并恢复会话，研究状态由 Orchestrator 从
  StateStore 恢复，二者互不依赖对方的内存。

## 3. 状态机设计

主链的「不可能违规」主要由三层状态机保证。所有状态转移
都写入 StateStore，转移条件即门禁规则。

### 3.1 数据集研究状态机

```
SELECTED ──► ADMITTING ──► REJECTED（准入不通过，终态）
                │
                ▼
            EXPLORING ──► PROBING_REJECTED（探针整集不通过，终态）
                │
                ▼
             ACTIVE ◄────┐ 批次循环（见 3.2）
                │        │
                ▼        │
      EXHAUSTED / COMPLETED（终态，可按 2.2.1 规则换集）
```

- 状态附带 `selection_mode`（manual / autonomous）与
  `locked` 标记：manual 选集进入 ACTIVE 后 locked=true，
  解锁条件（归因完成或用户解除）由 Orchestrator 在转移
  时校验；autonomous 选集的换集转移（→EXHAUSTED）必须
  附带穷尽证据条目引用（指向 MemoryStore 的禁止重跑
  清单条目）或用户命令记录，无引用则转移被拒。
- 同一时刻至多一个数据集处于 ACTIVE（串行约束）。

### 3.2 批次状态机

```
DRAFT_PREREG ──► LOCKED ──► VALIDATED ──► QUEUED ──► RUNNING
     │              │            │                        │
     └──► VOID      └──► VOID    └──► VOID                ▼
                                              RETRIEVING（找回对账）
                                                     │
                                                     ▼
                              CLOSED ◄── GATED ◄── ATTRIBUTED
```

- DRAFT_PREREG → LOCKED：预登记文件写入并计算内容哈希，
  哈希记入批次状态；LOCKED 之后预登记文件进入只读语义
  （见 §6.4），标准变更只能走 VOID + 新批次。
- LOCKED → VALIDATED：Validator 对全部表达式裁决完成
  且无 REJECT；存在 REJECT 时停留在 LOCKED 并报告，
  修改表达式须走 VOID（因为表达式清单属于预登记内容）。
- RUNNING → RETRIEVING：multisim 正常返回则直接通过；
  超时或子模拟缺失时进入找回流程（§6.3）。
- GATED：Gatekeeper 完成全批 Alpha 的门禁判定后转移。

### 3.3 Alpha 状态机

```
CREATED ──► SIMULATED ──► CLASSIFIED ──► BELOW_THRESHOLD（终态）
                              │
                              ▼ （Sharpe ≥ 1.58）
                          RA_CHECKED ──► RA_FAILED（fail>0，终态）
                              │ （fail=0）
                              ▼
                         CORR_CHECKED ──► CORR_REJECTED（终态）
                              │
                              ▼ （全部 < 阈值）
                          CANDIDATE
```

- 相关性检查的转移前置条件是 RA fail count = 0，这一
  条件同时在两层强制：Gatekeeper 的状态机不提供其他
  路径；Gateway 在收到 check_correlation 命令时二次校验
  目标 Alpha 的状态，非 RA_CHECKED(fail=0) 一律拒绝
  执行（双保险，对应验收 A2）。
- 平台 ERROR 的 Alpha 从 SIMULATED 直接转 CLASSIFIED
  并标技术错误，不进入后续门禁（FR-09 分类口径）。

---

## 4. 平台接口与能力白名单（设计落地）

### 4.1 Gateway 与 driver 协议

Gateway 与本项目的 adapter driver 使用 stdin/stdout 逐行 JSON 协议；driver
只负责把请求转给 `BRAIN_FLOW_HOME/scripts` 或共享只读会话，不持有第二套
BRAIN 认证与 Cookie 状态：

- 传输：子进程 stdin/stdout，逐行 JSON；请求
  `{"cmd": <命令>, ...参数}`，响应 `{"ok": bool, ...}`；
- Dataset、field 和 operator 目录通过 brain-flow 的现有脚本获取；
- 表达式准入、模拟、模拟恢复和最终 Alpha 审计分别使用
  `simulate_batch.py`、同一脚本的 `--resume` 和 `audit_alphas.py`；
- 没有脚本入口的必要只读查询，经 `brain_session.py` 共享会话执行；
- driver 或脚本异常时，Gateway 将失败交给 Orchestrator，由 §6.3
  恢复流程处理；任何不确定的提交状态都不得直接重发。

### 4.2 白名单命令表（FR-02 的设计产出）

| 命令 | 用途 | 备注 |
|---|---|---|
| get_datasets / get_datafields / get_operators | 现有 brain-flow 脚本目录查询 | 输出落在 brain-flow `runs/<case_key>/sessions/<run_id>/` |
| validate_simulation_settings | 检查本项目设置能否被脚本原样表达 | 不发平台请求 |
| simulate_batch | 调用 `simulate_batch.py` 准入并执行 | 不直连平台创建接口 |
| resume_simulation_batch | 调用 `simulate_batch.py --resume` | 只续接已保存的模拟 URL |
| audit_alphas | 调用 `audit_alphas.py --case-spec` | `--no-tag`，不改 Alpha 属性 |
| get_platform_setting_options / get_simulation_settings_schema | 只读平台设置查询 | 通过 `brain_session.py` |
| get_dataset_details / get_alpha_details / get_alpha_pnl / get_alpha_yearly_stats / check_correlation / get_user_alphas | 必要的只读回退与账户对账 | 通过 `brain_session.py` |
| set_case / shutdown | adapter 内部状态与关闭 | 不访问平台写接口 |
| shutdown | 关闭 driver（内部命令） | |

**不存在的命令**：submit 类能力不在表中、driver 不实现、
Gateway 不转发——提交在能力层面不可能（FR-02 红线）。
白名单的增删属于配置变更，须留痕；新增任何命令须先
评估其是否属于提交类或 scope 外能力。

## 5. 数据与文件设计

### 5.1 状态根与目录布局

项目内状态根为 `~/workspace/nastu_quant/`，布局如下：

```
nastu_quant/
├── config/            配置（§8）与变更日志
├── state/
│   ├── session_cookie.json   会话 cookie（0600，FR-01）
│   ├── research_state.json   主控状态（当前数据集/批次/授权模式）
│   └── stop.flag             急停标志（存在即停，FR-12）
├── memory/
│   ├── priors.md             先验库（人类可读台账）
│   ├── priors_index.json     先验结构化索引
│   └── do_not_rerun.jsonl    禁止重跑清单（含穷尽证据条目）
├── metering/
│   └── metering.jsonl        逐批计量记录（FR-15）
└── runs/
    └── {region}/{delay}/{universe}/{datasetid}/   研究产物（NFR-07）
```

凭证文件不在此树内：其路径由环境变量
`NASTU_QUANT_CREDENTIAL_FILE` 注入，系统只读不复制。

### 5.2 产物命名规范的细化（NFR-07 落地）

- 目录层级：`runs/{region}/{delay}/{universe}/{datasetid}/`，
  取值用平台原名（如 `GLB/D1/TOP3000/risk60/`）；
- 文件名语法：`{时间}_{字段组合}_{产物类型}.{格式}`，其中
  - 时间：批次启动时刻 `YYYYMMDD_HHMMSS`（Asia/Shanghai），
    同一批次的全部文件共享同一时间前缀，构成批次文件组；
  - 字段组合：本批表达式涉及字段以 `_` 连接（主字段在前），
    探针单字段文件只写该字段；
  - 产物类型：`probe_report` / `prereg` / `verdicts` /
    `results_raw` / `results` / `gate` / `metering`；
  - 格式集合：`md`（人读报告：probe_report、prereg、
    results）、`json`（机器读：results_raw 平台原始返回、
    verdicts 验证裁决、gate 门禁记录）、`jsonl` 仅用于
    全局台账类文件（metering、do_not_rerun）。
- 示例：`GLB/D1/TOP3000/risk60/20261023_142205_rsk60_offer_prereg.md`。

### 5.3 批次文件组（IF-03 四件套的落地）

每个批次在其数据集目录下产生一个文件组（同时间前缀）：
探针报告（probe_report.md，仅首批或字段变更时）、预登记
（prereg.md）、验证裁决（verdicts.json）、平台原始返回
（results_raw.json）、归因结果（results.md + 结构化指标
并入 results_raw 的提取层）、门禁记录（gate.json）、
本批计量（metering.json）。四件套的对应关系：探针报告、
预登记、结果 JSON、RESULTS 分别即 probe_report、prereg、
results_raw、results。

### 5.4 关键数据结构

- **research_state.json**：当前数据集及其状态机状态、
  selection_mode/locked、当前批次文件组前缀、各 Alpha
  状态的索引；Orchestrator 每次状态转移后原子写回
  （临时文件 + rename）。
- **预登记**：Markdown，固定小节——假设清单（编号、机制、
  预期符号、结构理由、证伪标准）、表达式清单（编号、
  表达式、对应假设）、字段差/比的放行理由（如有）。
  锁定后计算 SHA-256 记入批次状态，归因阶段先验哈希，
  哈希不符即判预登记被动过，按作废处理。
- **priors 条目**：结论、证据引用（批次文件组路径 + Alpha
  ID）、状态（active / voided + 作废原因与时间）。
- **do_not_rerun 条目**：数据集、机制描述、证伪证据引用、
  记录时间；穷尽判定直接引用条目集合。
- **metering 条目**：批次前缀、起止时间、时长、提交模拟
  数、找回/回退次数、token（可得时；不可得则字段值为
  null 并在汇报中呈现为「未统计」）。

## 6. 关键机制设计

### 6.1 认证与共享会话（FR-01）

BRAIN 登录和 Cookie 生命周期由 `brain-flow/scripts/brain_session.py`
独占管理。本项目通过 `BRAIN_CREDENTIALS_FILE` 提供凭证文件路径；凭证
内容不进入本项目的 JSON-lines 消息、状态文件或日志。认证失败时研究挂起；
不得通过重新发起模拟来恢复会话。

### 6.2 表达式验证集成（FR-07）

- 本地验证用于早期筛除；最终语法、算子和字段准入由
  `simulate_batch.py` 在发起前重新执行并写 admission receipt。
- Dataset 与字段证据由现有 `fetch_datasets.py` 和
  `inspect_fields.py` 生成；同一 case/run 的字段缓存由模拟脚本复核。
- 字段差/比放行：ResearchDesigner 在预登记中登记理由，
  Validator 以「理由非空且已登记」为放行条件，输出
  WARNING 并把理由写入 verdicts.json。
- 调用点只有一个：批次 LOCKED → VALIDATED 转移。
  Executor 只接受 VALIDATED 状态批次的表达式，接口上
  无旁路。

### 6.3 找回协议（FR-08）

1. 调用 `simulate_batch.py --resume <saved URL>` 续接同一个单模拟或
   MultiSim parent；相同输入和 admission receipt 必须保留；
2. 如果 runner URL 未能持久化，才用 `get_user_alphas` 做有界只读对账；
3. 账户记录不完整或无法唯一匹配时，批次保持 unresolved；不得单条回退重发；
4. runner 日志、URL、输入、结果与 read-back 证据均落在 case/run 目录。

### 6.4 预登记锁定（FR-06）

- 锁定动作 = prereg.md 写完 + 哈希入批次状态；此后
  Orchestrator 拒绝任何对该文件的写入路径（组件接口
  层面只读），归因前校验哈希。
- 作废动作 = 批次状态转 VOID 并记录原因，文件保留
  原样归档；新批次使用新的时间前缀重新起草。

### 6.5 授权模式与急停（FR-12）

- 授权模式存于 research_state.json（`auth_mode`：
  auto / per_batch），默认 auto；只能由 Reporter 接收
  用户明确指令后修改，每次修改记入配置变更日志。
- per_batch 模式下，Orchestrator 在 QUEUED 前插入
  APPROVAL 挂起态：Reporter 发出审批请求，收到明确
  肯定信号（IF-02）才放行；无信号则一直挂起。
- 急停：Reporter 收到停止指令即创建 state/stop.flag
  并通知 Orchestrator；Orchestrator 在每个批次与每个
  阶段转移前检查该标志，存在即停：不再发起任何新
  模拟，当前状态落盘，Reporter 回报停止确认。恢复
  运行须由用户明确指令并清除标志。

### 6.6 流量归因（FR-14）

- 每次提交成功后，Executor 立即以 set_alpha_properties
  给本系统 Alpha 写入名称标记 `nq_{datasetid}_{批次时间前缀}`，
  并登记台账（Alpha ID ↔ 批次 ↔ 表达式）。
- TrafficWatch 定期以 get_user_alphas 盘点：台账外的新
  Alpha 即外部流量；盘点发现来源不明活动时，Reporter
  报告用户，Orchestrator 暂停依赖账户盘点的判断
  （找回对账照常，但以对账键精确匹配，不误认）。

### 6.7 计量采集（FR-15）

采集点：批次起止（Orchestrator 状态转移时刻）、提交
计数（Executor）、找回/回退次数（Executor）、token
（harness 提供的运行统计，可得时写入；不可得写 null）。
Reporter 的批次汇报直接由 metering 记录生成，保证
数字实测可溯（NFR-06）。

---

## 7. 组件接口概要

| 接口 | 调用方 → 被调方 | 契约要点 |
|---|---|---|
| 会话保障 | 任意组件 → Gateway / brain-flow script | BRAIN 登录与 Cookie 由 `brain_session.py` 管理；恢复失败时调用方挂起 |
| 平台命令 | Gateway → adapter → script/session owner | §4.1 协议；模拟只能通过 `simulate_batch.py`，只读缺口通过共享 session |
| 准入 | Orchestrator → Admission | 返回 ADMIT/REJECT + 证据文件路径；REJECT 时主链终止于该数据集 |
| 探针 | Orchestrator → ProbeRunner | 返回探针报告路径 + 约束清单 + 准入判定；约束清单是 ResearchDesigner 的强制输入 |
| 预登记 | Orchestrator → ResearchDesigner | 起草 → 锁定（返回哈希）；锁定后仅提供只读与作废接口 |
| 验证 | Orchestrator → Validator | 输入表达式清单 + 预登记放行登记；返回逐条裁决；含 REJECT 时批次不得前进 |
| 执行 | Orchestrator → Executor | 仅接受 VALIDATED 批次；返回逐条 Alpha ID 与执行日志（含找回/回退记录） |
| 归因 | Orchestrator → Attribution | 输入 Alpha ID 清单 + 预登记；返回分类与假设回判，写入 results |
| 门禁 | Orchestrator → Gatekeeper | 逐 Alpha 按 §3.3 状态机推进；Candidate 授予写入 gate 记录与先验 |
| 播报 | 任意组件 → Reporter | 事件式：节点/异常/审批请求；Reporter 负责面向用户的呈现与信号识别 |

## 8. 配置设计（NFR-05 落地）

配置集中于 `config/nastu_quant.yaml`，变更记入
`config/change_log.md`（操作者、时间、内容、原因）。

| 配置项 | 现行值 | 变更级别 |
|---|---|---|
| scope.region / universe / delay | GLB / TOP3000 / 1 | 用户批准 |
| scope.decay / neutralization / truncation / pasteurization | 0 / MARKET / 0.08 / ON | 用户批准 |
| scope.is_window | 2014-01-01 至 2023-12-31 | 用户批准 |
| gate.sharpe_min | 1.58 | 用户批准 |
| gate.prod_corr_max / self_corr_max | 0.70（严格小于） | 用户批准 |
| gate.pairwise_corr_max | 0.40（严格小于） | 用户批准 |
| probe.windows | [5, 66] | 留痕即可 |
| exec.multisim_wait_threshold | 按平台常态设定 | 留痕即可 |
| auth.mode | auto（默认） | 用户操作（见 6.5） |
| auth.credential_env | BRAIN_CREDENTIALS_FILE | 用户配置 |

任一批次启动时，把当时配置快照写入批次文件组
（并入 gate.json 的 config 节），保证历史批次参数可
回溯。

## 9. 现行基线的改造清单

| 对象 | 保留 | 改造 |
|---|---|---|
| brain-flow scripts | Dataset/field/operator 目录、表达式准入、模拟、恢复与最终审计 | 通过 `BRAIN_FLOW_HOME` 和绝对 `BRAIN_PYTHON` 调用；不修改依赖仓库代码 |
| project adapter driver | JSON-lines 协议、命令白名单 | 不持有 BRAIN Cookie；只读缺口调用 `brain_session.py`，模拟只调用 `simulate_batch.py` |
| alpha_validator.py | 本地解析器、规则引擎 | 仅早期筛查；执行前的准入由 `simulate_batch.py` 复核 |
| AGENT_WORKFLOW_DESIGN.md | 角色与流程的早期构想 | 被本文取代，仅作历史参考 |
| runs/ 现行批次目录 | Batch 1–21 的既有产物 | 不迁移、不改名（历史归档）；新规范自 nastu_quant/runs 起执行 |

## 10. 异常处理矩阵

| 异常 | 检测 | 处理 | 对应验收 |
|---|---|---|---|
| 会话过期 | API 401 / driver 未认证 | §6.1 两级恢复；失败挂起报 needs_auth | A4 |
| multisim 卡死/子模拟缺失 | 等待阈值超时/数量不符 | §6.3 找回协议，禁止先重提 | A3 |
| driver 进程被杀/环境重启 | Gateway 通信失败 | 重建进程 → 会话恢复 → 状态恢复 → 找回对账后续跑 | A5 |
| 平台限流（429 等） | API 返回 | 退避等待，不放大重试（NFR-04） | — |
| 平台 ERROR（单条 Alpha） | 结果状态 | 归类技术错误，不计失败、不进证伪 | — |
| 预登记哈希不符 | 归因前校验 | 按作废处理并报告，禁止带病归因 | — |
| 陌生流量出现 | TrafficWatch 盘点 | 暂停相关判断，报告用户确认 | — |
| 用户急停 | stop.flag / 指令 | §6.5：停发新模拟，状态落盘 | A6 |
| 凭证文件失效 | 重连认证失败 | 同会话过期第二级失败处理 | A4 |

## 11. 需求追踪矩阵

| 需求 | 设计落点 |
|---|---|
| FR-01 认证与会话 | §6.1、§5.1、§4.1（reauth） |
| FR-02 能力白名单 | §4.2、§7（Gateway 校验） |
| FR-03 数据集准入 | C4、§3.1（ADMITTING） |
| FR-04 字段探索 | C5、§3.1（EXPLORING） |
| FR-05 假设管理 | C6、§5.4（预登记小节结构） |
| FR-06 预登记锁定 | §6.4、§3.2（LOCKED 与哈希） |
| FR-07 静态验证 | §6.2、§3.2（VALIDATED） |
| FR-08 执行与恢复 | §6.3、§3.2、§10 |
| FR-09 结果归因 | C9、§3.3（CLASSIFIED） |
| FR-10 RA Gate | §3.3、§4.1（Gateway 二次校验） |
| FR-11 记忆与先验 | §5.4（priors / do_not_rerun） |
| FR-12 审批与急停 | §6.5、§3.2（APPROVAL 挂起态） |
| FR-13 进度播报 | C12、§7（事件式播报接口） |
| FR-14 流量归因 | §6.6 |
| FR-15 运行计量 | §6.7、§5.4（metering 条目） |
| NFR-01 可恢复性 | §1.2 原则 2、§5.1、§10 |
| NFR-02 可审计性 | §5.3、§5.4（哈希与证据引用） |
| NFR-03 安全性 | §5.1、§6.1 |
| NFR-04 配额礼仪 | §6.3、§10（限流行） |
| NFR-05 可配置性 | §8 |
| NFR-06 诚实口径 | §6.7、§5.4（token null 口径） |
| NFR-07 命名规范 | §5.2 |
| IF-01 平台接口 | §4.1 |
| IF-02 用户接口 | C12、§6.5（信号要件） |
| IF-03 文件接口 | §5.3 |
| A1–A7 验收 | §11 各行对应的结构机制 + §10 异常矩阵 |

## 12. 设计决定与遗留事项

**本阶段作出的设计决定**（需求未规定、由设计层拍板，
供用户复核）：

- DS-1 状态根目录定为 `~/workspace/nastu_quant/`，历史
  runs/ 不迁移（§5.1、§9）；
- DS-2 命名规范细化为「时间前缀共享 + 产物类型后缀」
  的文件组形态（§5.2），是 NFR-07 在「一批多文件」
  现实下的落地解释；
- DS-3 相关性检查采用 Gatekeeper 状态机 + Gateway
  接口校验双层强制（§3.3）；
- DS-4 本系统 Alpha 以 set_alpha_properties 名称标记
  做流量归因（§6.6）；
- DS-5 配置采用单文件 + 变更日志形态（§8）。

**遗留到实现阶段的事项**：

- driver 改造（§9）的具体代码变更与回归测试；
- Validator 库化调用的封装；
- multisim 等待阈值的实测标定（先按现行经验值，
  运行中按 NFR-05 留痕调整）。

---

*本文档状态：已定稿（v1.0，2026-10-23 用户确认）。下一步进入实现阶段。*

*定稿后附记（2026-10-23）：用户指定实现代码仓库为
https://github.com/Justices/alpha_explore_agent.git （本地克隆于
~/workspace/alpha_explore_agent/），全部实现代码保存于该仓库；
运行时状态（含会话 cookie）仍按 §5.1 位于仓库之外的
nastu_quant/ 状态根，不进版本控制。*
