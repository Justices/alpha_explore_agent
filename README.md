# alpha_explore_agent — nastu_quant

nastu_quant 是一个在 WorldQuant BRAIN 平台上自主探索 Alpha 的
Agent 系统：从数据集准入、字段探针、假设与预登记，到回测执行、
结果归因与 RA Gate 判定，构成一条门禁由代码与状态机保证、
不依赖执行者自觉的研究流水线。Alpha 提交永远由用户人工完成，
系统在能力层面不具备提交功能。

## 文档

- 需求规格说明书（已定稿 v1.0）：工作区 `AGENT_REQUIREMENTS.md`
- 设计文档（已定稿 v1.0）：工作区 `AGENT_DESIGN.md`

## 布局

```
src/nastu_quant/
├── driver.py       平台驱动：JSON-lines 协议 + 命令白名单 +
│                   两级会话恢复（cookie → 凭证文件），无提交能力
├── gateway.py      Gateway：白名单校验 + 相关性检查的 RA 前置校验
├── validator.py    表达式静态验证（解析器 + 规则引擎，带拒绝码）
├── gatekeeper.py   Alpha 状态机与门禁判定（阈值严格小于口径）
├── recovery.py     multisim 找回对账协议（先找回，禁先重提）
├── state_store.py  落盘状态、预登记哈希锁定、急停标志
├── config.py       显性配置与变更留痕
├── metering.py     逐批计量（时长/模拟数/token，不可得记 null）
└── paths.py        状态根与 NFR-07 产物命名规则
tests/              离线测试（假平台桩，无网络、无真实凭据）
```

运行时状态（配置、先验、产物、会话 cookie）位于代码仓库之外
的 `~/workspace/nastu_quant/` 状态根，凭证文件由环境变量
`NASTU_QUANT_CREDENTIAL_FILE` 注入路径，均不进版本控制。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```
