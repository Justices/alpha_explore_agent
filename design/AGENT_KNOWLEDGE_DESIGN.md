# nastu_quant 知识层设计：检索、会话与失败诊断优化

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0（已定稿，2026-10-27） |
| 日期 | 2026-10-26（定稿 2026-10-27） |
| 定位 | AGENT_DESIGN.md 的补充设计：知识层（C15）、会话与决策日志、失败诊断与优化流程 |
| 依据 | AGENT_REQUIREMENTS.md v1.0、AGENT_DESIGN.md v1.0、仓库现状代码（agent.py / model_client.py / gatekeeper.py，截至 commit ec4e748） |
| 状态口径 | 本文中「现状」指已拉取代码的实测形态；「设计」指待实现部分，两者严格区分 |

---

## 1. 引言

### 1.1 目的

本文档为 nastu_quant 补上三块尚未落地的设计：

1. **知识检索层（RAG）**：让模型的经济决策能用上自维护知识
   （研究规范、构建建议、算子笔记、先验与历史批次证据），且用法
   受本系统既有铁律约束，而不是通用问答式检索。
2. **会话与决策日志**：明确「同一个 agent 跨启动的 session」
   由什么构成、保存在哪、如何恢复，并补上模型决策的可审计日志。
3. **失败诊断与优化流程**：Alpha 在门禁处失败后，系统化地回答
   「哪一项回测检查失败、机理是什么、有哪些结构级补救、补救如何
   以合规方式进入下一批次」，把现在散落在逐批归因里的人工判断
   沉淀为可复用的诊断与补救知识库。

### 1.2 现状（写设计前已核对代码）

- `model_client.py`：模型是无状态 JSON 决策服务，接口为
  `complete_json(task, context)`，共 5 个决策点（select_scope、
  select_dataset、plan_probes、construct_batch、attribute_results）。
  静态指令在 `TASK_INSTRUCTIONS` 中，知识文档尚未接线进 context。
- `agent.py`：单批流水线已实现「产物即会话」——批次目录内的
  prereg.json（哈希锁定）、simulation_intent.json（SUBMITTING →
  READ_BACK）、probe 与 results 产物构成全部恢复依据；
  `recover()` 先验哈希、再按表达式与平台对账找回；`run_once`
  遇未完成批次拒绝开新批。全局仅有 runs 根下的 alpha_states.json。
- `gatekeeper.py`：`evaluate_candidate` 已输出结构化判定，含完整
  的 RA 检查清单（LOW_SHARPE、LOW_FITNESS、HIGH_TURNOVER、
  LOW_TURNOVER、LOW_RETURNS、CONCENTRATED_WEIGHT、LOW_2Y_SHARPE、
  IS_LADDER_SHARPE、区域 Sharpe 系列、LOW_SUB_UNIVERSE_SHARPE、
  LOW_ROBUST_UNIVERSE 系列、LOW_AFTER_COST_ILLIQUID 系列、
  LOW_INVESTABILITY_CONSTRAINED 系列等），是失败诊断的现成输入面。
- 尚不存在：知识层组件、模型调用日志、campaign 级状态、
  失败诊断与补救库。

### 1.3 范围外

- 不改变 FR-01～FR-15 的任何口径；本文与定稿冲突时以定稿为准，
  需改口径的事项一律列入第 9 节决策记录。
- 不引入对话式长会话模型（理由见 5.1）。
- 平台事实（数据集、字段、指标）永远以 live API 为唯一来源，
  知识层不缓存、不替代（见原则 P4）。

---

## 2. 设计原则

| 编号 | 原则 | 说明 |
|---|---|---|
| P1 | 规则注入，不靠检索 | 规范类知识是义务不是参考：按决策点确定性整节注入，不走相关性检索，避免漏检即违规 |
| P2 | 证据必溯源 | 检索返回的每条证据带来源文件、小节、日期、有效性状态；模型引用无出处的「知识」视为臆造，按证据不足处理 |
| P3 | 作废必过滤 | 先验与补救条目都有生命周期状态；已作废/已证伪条目不得作为有效证据被检索出，只能以「已作废」标记显式呈现 |
| P4 | live 事实不入索引 | 索引只含自维护文档与本系统产物；平台目录、指标、状态永不从索引取 |
| P5 | 诊断不改判定 | 失败诊断只解释与建议，不改变本批预登记、门禁结论与分类；诊断产物与判定产物分开落盘 |
| P6 | 优化走新批次 | 任何补救只能以「新批次 + 新预登记」形式落地，理由字段必须引用诊断编号；禁止在本批内改标准重跑，禁止以回测分数为目标的逐参数搜索 |
| P7 | 确定性优先 | 知识层与诊断的匹配逻辑是代码而非模型：同输入同输出、可单测；模型只在代码给定的候选集合内做机理叙述与取舍 |

---

## 3. 知识分层与语料

知识按性质分三层，层的处理方式互不通用：

| 层 | 内容 | 交付方式 | 更新方式 |
|---|---|---|---|
| L1 规范层 | 研究工作流规范、构建建议（回测设置/模型选择/窗口与日期依赖/优化方法论）、需求与设计定稿中的规则口径 | 按决策点映射整节注入 context（映射表见 4.4） | 文档定稿即生效；变更走配置变更留痕 |
| L2 查表层 | 103 个算子的经济含义与选用条件、字段目录快照（仅作名称/类型校验参考，不作事实来源） | 精确查表接口（按算子名/字段 id） | 算子笔记修订时更新；查不到即报错，不猜测 |
| L3 证据层 | 先验库、逐批 RESULTS 与探针报告、诊断与补救记录（第 6 节产生）、作废留痕 | 段落级检索（见 4.3），结果带元数据与有效性过滤 | 每批关闭时回写；旧结论被推翻时标作废，不删 |

语料规模判断：当前自维护文档与产物合计为数百 KB 量级、结构
规整（标题层级稳定、每批目录自含结论）。此规模下**不预设向量
数据库**；检索实现分两阶段（4.3），向量检索仅在触发条件满足
时才立项评估，避免为检索而检索。

---

## 4. 检索服务（新组件 C15 KnowledgeService）

### 4.1 定位

KnowledgeService 与 Validator 同级：agent 代码调用的确定性
组件，模型不直接检索。输入是结构化查询（决策点 + 键/标签），
输出是可直接进 context 的知识包（knowledge pack），每个片段
自带出处。全部调用结果随批次落盘，使每次模型决策用过哪些
知识可回溯（NFR-02）。

### 4.2 接口

```text
inject_rules(task) -> [片段]            # L1：按 4.4 映射表整节注入
lookup_operator(name) -> 条目 | None    # L2：精确查表
lookup_field_note(field_id) -> 条目|None # L2：字段备注（若有）
search_evidence(query_tags, k) -> [证据片段]  # L3：标签+关键词检索
```

`query_tags` 为结构化标签而非自由文本，至少含：
`dataset_id`、`mechanism`（机制族）、`failure_signature`
（失败签名，见 6.2）、`field_generation`（字段生成机制类别）。

### 4.3 证据条目的元数据与检索方式

每条 L3 证据在入索引时必须携带：

| 字段 | 说明 |
|---|---|
| source | 来源文件路径 + 小节/条目编号 |
| date | 结论产生日期 |
| validity | 有效 / 已作废 / 已证伪 / 待验证 |
| tags | dataset × mechanism × 失败签名等结构化标签 |
| scope | 结论成立的回测设置范围（region/delay/universe） |

检索规则：

- **有效性过滤先于相关性排序**：validity 非「有效」的条目默认
  不返回；仅当查询显式要求历史教训时，才以「已作废/已证伪」
  标记返回，且不得与有效条目混排。
- 阶段一实现：文档按标题层级切分 + 标签索引 + 关键词（BM25
  或等价词频法）排序。要求同语料快照下结果确定、可复现。
- 阶段二（向量检索）触发条件（满足其一才评估立项）：证据条目
  超过约 2000 条且阶段一的抽查命中率明显不足；或跨措辞同义
  查询成为主要失败模式。启用向量时仍保留元数据过滤与出处，
  且 embedding 模型版本变更必须重建索引并留痕。

### 4.4 L1 注入映射（决策点 × 知识节）

| 决策点 | 注入内容 |
|---|---|
| select_dataset | 工作流规范的数据集选择与锁定规则；先验中各候选数据集的状态摘要（L3） |
| plan_probes | Step 2 字段探索全节（P1–P6 定义与约束翻译要求） |
| construct_batch | 构建建议：模型选择（按类别 neutralization / 信号结构按字段生成机制）+ 时序窗口节；相关算子条目（L2，按表达式草案涉及算子查表）；同机制历史证据与失败教训（L3） |
| attribute_results | 四分法分类口径；失败诊断的签名与补救候选（第 6 节） |

---

## 5. 会话与决策日志

### 5.1 三层 session 的定义与现状

「同一个 LLM agent 启动的 session」在本系统中拆为三层，
混为一谈是多数实现恢复困难的根源：

| 层 | 内容 | 现状 | 保存方式 |
|---|---|---|---|
| 平台会话 | BRAIN 登录态 | 已实现 | driver 两级恢复：项目内 cookie（0600）→ 项目外凭证文件 |
| 模型会话 | 与模型的对话上下文 | **刻意不存在** | 模型为无状态决策服务：每次 `complete_json(task, context)` 的上下文由代码现组 |
| 工作会话 | 研究进行到哪一步 | 单批已实现，跨批缺失 | 单批 = 批次目录产物（见 5.2）；跨批 = campaign 状态（见 5.3） |

维持模型无状态的理由：① 模型可替换（provider/model 变更不丢
研究状态）；② 每次决策的输入可完整落盘复现，不存在上下文
漂移与「它当时为什么这么想」的不可考；③ context 长度不构成
研究长度的上限。若未来引入交互式会话，其产物也必须沉淀为
决策日志与状态文件后才算数，不以对话 transcript 为准。

### 5.2 单批会话（已实现，本文只登记口径）

批次目录即会话快照：prereg.json + state.json（哈希）、
simulation_intent.json（状态机）、probe_*、results.json、
gate.json。恢复协议 = `recover()`：验哈希 → 按对账键与平台
核对 → 只补读回，不重提。进程内不持有唯一状态，任意启动
（新进程、新模型、新 harness）读同一目录即续同一会话。
对账的 settings 比对口径（OD-K1 已定，见第 9 节）：平台记录
缺失某个 settings 键不构成否决，缺失键在 recovery 结果中
留痕；记录中存在该键但取值与 intent 不一致，才否决该记录。

### 5.3 跨批会话缺口：campaign 状态（待实现）

多批次连跑（一轮 campaign）需要一份 campaign 级状态，
即 AGENT_DESIGN 中状态根 `state/research_state.json` 的落地：

- 内容：当前数据集与数据集状态机位置、选集模式与锁定位、
  当前批次指针、Candidate 台账引用、授权模式、schema 版本。
- 纪律：单写者（启动时取锁，锁文件含进程与时间；发现他人
  持有即拒绝启动并报告，不抢占）；原子写（沿用
  `atomic_write_json`）；schema 变更只增不改语义，旧版本可读。
- 与单批会话的关系：campaign 状态只存指针与状态机位置，
  事实细节永远在批次产物里（避免双写不一致）。

### 5.4 模型决策日志（待实现，本设计的会话记忆载体）

`model_client` 外包一层日志包装（或由 agent 调用点统一记录），
每次 `complete_json` 追加一行到状态根 `logs/model_log.jsonl`：

| 字段 | 说明 |
|---|---|
| ts | 调用时刻 |
| batch_ref | 批次目录（若在批次内） |
| task | 决策点名 |
| context_sha256 / context_ref | context 哈希 + 落盘引用（context 原文随批次产物保存，日志只存索引） |
| knowledge_pack_ref | 本次注入的知识包引用（4.1） |
| response_sha256 | 响应哈希（原文随批次产物保存） |
| token_in / token_out | 模型服务回传时记录；不可得记 null（FR-15 口径：未统计，不估计） |
| latency_ms / provider / model | 计量与复现用 |

用途：审计回放（任一决策可由 context 原文重演）、FR-15 计量
数据源、跨启动的「会话记忆」——新启动的 agent 读日志索引即可
知道此前每次决策的输入输出，而不需要任何对话上下文。

---

## 6. 失败诊断与优化流程

### 6.1 定位与时机

诊断发生在批次流水线的门禁判定之后、与归因同期：新增阶段
**Diagnose**（agent._complete 之后、批次关闭之前）。输入是
`evaluate_candidate` 的结构化输出与本批全部产物；输出是
`diagnosis.json`（随批次落盘）与给下一批次的补救建议。
诊断不改变本批任何判定（原则 P5）。

### 6.2 失败签名

诊断的第一步是确定性签名匹配（代码执行）：把单个 Alpha 的
失败集合映射为**失败签名**——单项签名与组合签名两类。

单项签名（与 gatekeeper 的检查清单一一对应）：

| 签名 | 触发检查 | 常见机理（待证据校准） |
|---|---|---|
| S_SHARPE | LOW_SHARPE | 机制本身弱或方向反；信号被噪声淹没 |
| S_FITNESS | LOW_FITNESS | Sharpe 与换手/收益的组合不达标，常与高换手或低收益共现 |
| S_TURNOVER_HI | HIGH_TURNOVER | 信号变化快于字段真实变化（窗口过短、稀疏跳变、未平滑） |
| S_TURNOVER_LO | LOW_TURNOVER | 信号过慢或几乎不变（水平型字段误用长窗平滑） |
| S_CONCENTRATED | CONCENTRATED_WEIGHT | 少数标的权重过大（结尾保留幅度且分布厚尾、未截断） |
| S_2Y | LOW_2Y_SHARPE | 近两年失效：机制衰减或制度依赖（regime 依赖） |
| S_LADDER | IS_LADDER_SHARPE | 分年不稳定：少数年份贡献全部收益 |
| S_REGION | LOW_GLB_AMER/EMEA/APAC、LOW_ASI_JPN | 机制只在部分区域成立（覆盖、微结构、披露制度差异） |
| S_SUBUNIV | LOW_SUB_UNIVERSE_SHARPE | 信号只在大票/子宇宙外成立，子宇宙内无信号 |
| S_ROBUST | LOW_ROBUST_UNIVERSE_* | 换稳健宇宙后失效：信号依赖流动性差的小票 |
| S_AFTERCOST | LOW_AFTER_COST_ILLIQUID_* | 成本后失效：换手 × 非流动性成本吞掉毛收益 |
| S_INVEST | LOW_INVESTABILITY_CONSTRAINED_* | 可投资性约束后失效：与 S_ROBUST/S_CONCENTRATED 常共现 |
| S_RETURNS | LOW_RETURNS | 方向对但幅度小：收益本身不足（常与 S_FITNESS 共现） |

组合签名（多项共现才成立，机理判断更强）：

| 签名 | 组合 | 解读 |
|---|---|---|
| C_DECAY | Sharpe 过线 + S_FITNESS + S_2Y | 信号在衰减：早期有效、近年被磨平或被套利 |
| C_COST | Sharpe 尚可 + S_TURNOVER_HI + S_AFTERCOST | 换手成本型：机理可能真，交易实现吃掉收益 |
| C_COVER | 主 Sharpe 行 + S_SUBUNIV / S_ROBUST | 覆盖集中型：信号只在部分股票池成立 |
| C_SINGLE_YEAR | S_LADDER + S_2Y | 单一年份/单一 regime 驱动，非稳健机制 |

历史锚点（验收用，见第 8 节）：2rmYJ1oJ（Sharpe 1.64）应被
签为 S_FITNESS + S_2Y（实测 fitness 0.65、2Y 0.98）；
mL6vP25E 应在诊断之前就被 validator/gate 拦截（违规拼接 +
RA 未过不属诊断对象，诊断不得为违规样本提供补救）。

### 6.3 补救方案库（Remedy KB，L3 证据层的特殊子集）

每条补救是一个结构化条目（JSON，随知识层版本管理）：

| 字段 | 说明 |
|---|---|
| remedy_id | 稳定编号 |
| applies_to | 适用的失败签名（单项或组合） |
| preconditions | 前置条件（字段速度类别、分布、覆盖等探针事实须满足什么） |
| action | 结构级动作（见 6.4 动作空间），含具体改法 |
| rationale | 为什么该动作针对该机理（经济/统计理由） |
| expected_effect | 预期改善哪项检查、可能恶化哪项（写明代价） |
| evidence | 历史批次引用（批次目录 + alpha id + 实测变化） |
| status | 种子（来自人工经验）/ 已验证 / 已证伪 / 已作废 |
| uses / successes | 被采用次数与事后判定有效的次数（由回写更新） |

### 6.4 补救动作空间（只允许结构级动作）

补救动作限定在「换一种结构表达同一机制」的范围内，
与构建建议的算子框架一致：

1. **平滑类**：对快/稀疏字段加 ts_mean / ts_decay_linear 平滑
   或拉长窗口（scope 的 decay 锁定为 0，换手治理只能在表达式
   内做——历史教训：未平滑的稀疏流量字段换手 > 1.2 不可交易）。
2. **窗口类**：delta 窗口与字段变化节奏对齐（如费率类字段
   5 日变化仅两成、66 日才六成——短窗 delta 是纯噪声）。
3. **结尾算子类**：rank ↔ zscore 家族互换（信排序还是信幅度）、
   signed_power 压缩/放大；厚尾分布先洗单位再比较。
4. **分组类**：neutralization 分组层级调整（market ↔ sector ↔
   subindustry）——历史实证：某短反转信号 subindustry 中性化
   1.34 对 market 的 1.13。
5. **字段取舍类**：剔除近重复字段、换同机制的更稠密字段、
   稀疏字段改用「是否活跃」类事件表达或直接判弃。
6. **机制再审类**：方向/机制本身被证伪时，补救不是修参数，
   而是标记假设证伪并退出（计入穷尽证据），禁止换皮重跑。

明确禁止作为补救：逐个试窗口/参数的网格搜索、改回测设置
迁就结果（scope 锁定）、对同一表达式微调后重提（与禁止重跑
清单冲突的由 MemoryStore 拦截）。

### 6.5 诊断流程（Diagnose 阶段）

```text
gate.json（逐 Alpha 失败集合）
  → 代码：签名匹配（6.2）+ 结构特征提取（结尾算子族/窗口/
    字段速度/分组层级，来自 prereg 与表达式解析）
  → 代码：按签名查 Remedy KB，过滤前置条件不满足者
  → 模型：complete_json("diagnose_failures", {签名, 候选补救,
    本批证据}) 在候选内做机理叙述与排序，给出逐 Alpha 建议：
    放弃 / 以某补救开新批 / 机制再审
  → 落盘 diagnosis.json（失败项在前）+ 回写 L3 证据层
```

模型在此阶段同样无权引入候选集之外的补救（与 construct_batch
的「不得臆造探针证据」同纪律）；候选为空时诊断结论只能是
「无已知补救」，这本身是穷尽证据的一部分。

### 6.6 优化闭环与护栏

1. 采纳补救 = 新批次：新 prereg 的理由字段必须含
   `remedy_id + diagnosis 出处`；新批次照常过 validator、
   预登记锁定与全部门禁。
2. **修复轮次上限**：同一假设谱系（同一数据集 × 同一机制）
   最多 2 轮补救批次（OD-K2 已定）；超限即判该谱系穷尽
   （记证据），额外轮次只能由用户命令开启（与数据集换集的
   非对称规则同口径：「结果不好」不构成续跑理由）。
3. 补救回写：补救被采用后，其成败在新批次归因时回写 KB
   （uses/successes 与 status 流转）；连续证伪的补救降级为
   已证伪并注明出处——补救库与先验库同一条「可推翻、要留痕」
   纪律。
4. 诊断与穷尽：当某数据集全部机制谱系均已证伪或修复超限，
   Diagnose 的汇总输出即为 FR 口径下的穷尽证据条目来源之一，
   供数据集状态机换集判定引用。

---

## 7. 与现有代码的衔接（改造清单）

| 项 | 改动 | 备注 |
|---|---|---|
| 新增 `knowledge.py` | KnowledgeService：L1 注入映射、L2 查表、L3 检索与知识包组装 | 纯确定性组件，独立单测 |
| 新增 `diagnosis.py` | 签名匹配、结构特征提取、Remedy KB 查询与回写 | 输入只读 gate.json 与 prereg，不碰判定 |
| 新增知识数据 | 状态根 `knowledge/`：remedies.json（补救库）、evidence 索引（由语料构建，可再生） | 索引是缓存，源文件是事实；索引损坏可重建 |
| `agent.py` | _complete 后新增 Diagnose 阶段；construct_batch / attribute_results 的 context 组装时调 KnowledgeService | 批次产物新增 diagnosis.json、knowledge_pack.json |
| `model_client.py` | 新增 `diagnose_failures` 的 TASK_INSTRUCTIONS；调用点加决策日志包装（5.4） | 指令口径：只在候选集内取舍，不得臆造补救 |
| `state_store.py` | campaign 状态读写与启动锁（5.3） | 与单批 state 分开文件，避免双写 |
| `gatekeeper.py` | 不改判定；evaluate_candidate 的 failures 输出即诊断输入 | 若字段不足以定位限值差距，诊断侧容忍缺失并标注 |
| 说明 | 本节为实现期清单 | campaign 状态（5.3）与决策日志（5.4）均列入二期范围（OD-K3）；知识/诊断组件先独立单测，随二期接线一并接入（OD-K6）；Remedy KB 种子须先提取审定（OD-K4） |

## 8. 验收要点（设计级，实现后逐条实测）

| 编号 | 验收项 |
|---|---|
| K1 | L1 注入确定性：同一决策点、同一文档版本下，两次组装的知识包逐字一致 |
| K2 | 作废过滤：构造一条已作废先验，常规检索不得返回；显式历史查询返回时必须带「已作废」标记 |
| K3 | 诊断复现：以 2rmYJ1oJ 的实测指标构造输入，签名必须为 S_FITNESS + S_2Y；以 mL6vP25E 输入，诊断必须拒绝受理（违规样本不进诊断） |
| K4 | 补救闭环留痕：任一采纳补救的新批次，其 prereg 可溯源到 diagnosis 编号与 remedy_id；缺失引用即校验失败 |
| K5 | 决策日志完整：一次完整批次后，model_log.jsonl 的记录数与实际 complete_json 调用数一致，token 不可得处为 null |
| K6 | 跨启动等价：批次中途杀进程，新进程凭批次目录 + campaign 状态恢复，最终产物与不中断运行等价（与既有 A5 同口径） |

## 9. 决策记录（2026-10-27 用户确认定稿时一并拍板）

| 编号 | 问题 | 已定口径 |
|---|---|---|
| OD-K1 | match_alphas 的 settings 缺失键口径 | **缺失不否决，现存不一致才否决，缺失键留痕**：平台记录缺某个 settings 键时不因此被过滤，缺失键记入 recovery 结果；记录中存在该键但取值与 intent 不一致时硬否决。实现按此改；测试夹具改为平台真实键形（camelCase），不改实现迁就夹具 |
| OD-K2 | 修复轮次上限与超限处置 | 上限 **2 轮**维持；超限判谱系穷尽（记证据），额外轮次只能由用户命令开启 |
| OD-K3 | campaign 状态（5.3）与决策日志（5.4）的实现范围 | **两者都列入二期**（K5/K6 验收与补救回写以此为落点）；若砍范围，先砍 L3 向量检索 |
| OD-K4 | Remedy KB 种子条目审定 | 种子先从 Batches 1–21 的 RESULTS 逐条提取，再由**用户审定**后才进实现；未审定的条目保持「待证据校准」，不得当作已验证使用 |
| OD-K5 | 向量检索触发阈值 | 约 2000 条证据条目的暂定阈值**维持不变**（当前语料远未及触发线） |
| OD-K6 | 知识层与二期的先后 | 知识/诊断组件**先独立单测**，随二期接线一并接入，不单独先行集成 |

---

## 附录 A：种子补救条目（示例，状态均为「种子/待验证」；按 OD-K4，须从 Batches 1–21 的 RESULTS 逐条提取并经用户审定后才可进入实现）

| remedy_id | 适用签名 | 动作 | 历史证据 |
|---|---|---|---|
| R-TURN-SMOOTH | S_TURNOVER_HI、C_COST | 对稀疏/快字段加 ts_mean 平滑后再构造 | risk60 crowding 原始换手 1.61，探针阶段即判定须平滑 |
| R-WINDOW-ALIGN | S_TURNOVER_HI、S_SHARPE | delta 窗口对齐字段变化节奏（慢字段 ≥66 日） | risk60 费率：5 日变化 21–26%、66 日 62–65% |
| R-GROUP-LEVEL | S_REGION、S_SHARPE | 调整分组层级（market → subindustry 等） | Batch 8：同一短反转信号 1.13（market）→ 1.34（subindustry） |
| R-ENDING-SWAP | S_CONCENTRATED、S_SHARPE | 结尾算子在 rank/zscore 家族间互换，或 signed_power 压缩 | 构建建议的结尾算子选择框架（幅度 vs 排序） |
| R-FIELD-DROP | S_SHARPE、C_COVER | 剔除近重复或过稀疏字段，必要时判弃整集 | risk60 bid≈offer 近重复：利差假设探针阶段否决 |
| R-MECH-REVIEW | C_DECAY、S_2Y | 机制再审：区分「机制死亡」与「表达不当」，前者直接判证伪退出 | 「变化胜水平」先验被载荷类批次推翻的修订史 |

*本文档状态：已定稿（v1.0，2026-10-27 用户确认，OD-K1～K6 口径见第 9 节）。按第 7 节清单进入实现排期。*
