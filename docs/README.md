# 运行与验收文档

**统一入口**：[系统整体设计](../design/CURRENT_DESIGN.md)。技术文档不授予真实 BRAIN 或 Provider 执行权限；规范性研究范围见 [RESEARCH_SCOPE.md](../design/RESEARCH_SCOPE.md)，安全规范见 [AGENTS.md](../AGENTS.md)。

| 主题 | 当前入口 | 内容 |
| --- | --- | --- |
| 运行与运维 | [OPERATIONS.md](OPERATIONS.md) | 本地只读诊断、隔离 Authority 演练、容器部署注意事项、凭证与故障处理 |
| 测试与发布 | [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md) | 跨模块验证矩阵、真实生产阻断项、CI 与风险分类 |
| 业务与模块 | [CURRENT_DESIGN.md](../design/CURRENT_DESIGN.md) | 三种业务流程、角色、知识、模型、BRAIN、权限、费用、恢复、Candidate |
| 低层组件 | [RESEARCH_ARCHITECTURE_V1_1.md](../design/RESEARCH_ARCHITECTURE_V1_1.md) | 组件职责与依赖边界 |

不再按历史开发批次维护多个同义的“总体设计”或“上线通过”文件。历史迭代审计与当时的操作示例留在 [archive](archive/README.md)，仅供追溯；**历史成功结论不得直接迁移为当前提交的验收证据**。
