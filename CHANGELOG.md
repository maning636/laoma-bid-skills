# Changelog

本文件记录 `laoma-bid-skills` 的公开变更。

## [0.1.0] - 2026-11-26

### Added

- 首发 8 个确定性投标文档处理技能：Word 排版、暗标脱敏、规则筛查、检查报告导出、风险评分、报价核查、自检清单与自评分估算。
- 提供统一的 `Skill` / `SkillContext` / `SkillResult` 契约、规则包读写与坏 JSON 修复工具。
- 提供安装、快速开始、Agent 接入、能力边界与 AGPL-3.0 归属说明。

### Fixed

- 将 GitHub Actions 扩展为 Python 3.10、3.11、3.12 矩阵，并在每个版本执行 Ruff 与完整测试。
- 在开发依赖中补充 `pytest-asyncio`，确保异步 golden tests 可在干净环境收集。
- 修复全部 113 项 Ruff 诊断，保留技能运行契约与现有 81 条测试。
- 将 README 的静态测试徽章替换为真实 CI 徽章，并补全暗标脱敏示例导入。
- 移除仅供内部维护使用的上线任务书，避免公开用户误读为待办事项。

### License

- 保持 AGPL-3.0-only，保留 `LICENSE` 与 `NOTICE` 中的上游归属声明。
