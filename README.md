# Exact Cut

Exact Cut 是短剧素材制作工作台：在网页填写剧情片段、字幕和结尾，后台准备原片、精确剪辑与质检，预览后按需剪切打码，再由用户确认交付 FBBM。

## 从这里开始

| 要做什么 | 阅读入口 |
| --- | --- |
| 使用工作台 | [用户操作说明](docs/user-guide.md) |
| 安装依赖、启动开发、运行测试 | [开发说明](source/README.md) |
| 了解代码结构、后台任务与并发 | [当前架构](docs/architecture.md) |
| 发布、回滚、迁移 Linux 服务 | [部署与迁移](docs/linux-migration.md) |
| 查看合并结果、遗留问题和线上版本依据 | [项目状态](docs/status.md) |
| 查其他说明或历史资料 | [文档目录](docs/README.md) |

## 仓库结构

- `source/`：应用、网页资源、配置模板、测试和部署工具。
- `docs/`：日常说明、项目状态和运维记录索引；历史资料统一放在 `docs/archive/`。
- `integrations/`：随容器构建的物料集成脚本。
- `configuration/`、`operations/`、`evidence/`、`history.bundle`、`MANIFEST.sha256`：最初交接包的配置示例、工具及证据快照。它们不表示当前 main 或线上版本已验收。

开发代码、测试数据和生产数据独立。Git 合并不等于上线；生产变更须单独授权并记入[线上变更索引](docs/production-change-log.md)。
