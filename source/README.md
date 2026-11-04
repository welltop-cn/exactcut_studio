# 开发说明

日常文档见[文档目录](../docs/README.md)，处理流程与资源划分见[当前架构](../docs/architecture.md)。本文件只维护安装、开发和测试入口；线上操作见[Linux 部署与迁移](../docs/linux-migration.md)。

## 环境与安装

应用要求 Python 3.11+、FFmpeg 和 ffprobe。本工作区按用户约定使用 Conda 的 Python 3.12 解释器，通过 `EXACTCUT_PYTHON` 指定；不要把个人解释器的绝对路径提交到仓库。

在仓库根进入应用目录。下面的 `python3.12` 应能从当前 PATH 找到，或在本机替换为约定解释器：

```sh
cd source
EXACTCUT_PYTHON=python3.12 ./bin/setup
cp .env.development.example .env.development
```

`bin/setup` 创建 `.venv`，安装开发锁文件和本项目。已有开发环境先核对依赖，不必重复初始化。

| 文件 | 用途 |
| --- | --- |
| `requirements.lock.txt` | 运行依赖；生产镜像使用 |
| `requirements-dev.lock.txt` | 运行依赖加测试工具；开发及验证镜像使用 |
| `requirements-sam2.txt` | 可选 CPU SAM 依赖；模型与配置另行准备 |

UI 测试还需 Playwright 与匹配的 Chromium；部分测试需要 Node、Git 和真实媒体工具。Playwright 未列入当前开发锁文件，验收环境须另外准备并记录版本。缺工具导致跳过不能称为已经通过对应验收。

## 启动本地开发

先读[本地服务清单](../docs/local-dev-services.md)，核对是否已有服务、其他会话及活动任务，再启动：

```sh
./bin/dev-web
```

默认入口为 `http://127.0.0.1:8793/home/`，数据在 `source/.dev-state/data`，初始管理员密码保存在该数据根的 `.web-state/password`，不在文档或日志中公开。

启动器使用独立 `.env.development`、集中配置和数据目录，清除继承的生产服务凭据。普通 worker 开启，下载、AI、发布和外部写入默认关闭；它不是旧文档中的“仅静态页面、worker 关闭”模式。

已有的 `--cutting`、`--title-images`、`--fetch-env` 选项用于不同专项配置，使用前检查 [bin/dev-web](bin/dev-web) 和当前服务清单。不要为查看页面直接接入生产数据或开启真实交付，也不要在运行中的测试树上切换代码。

## 测试

以下命令均在 `source/` 下执行。修复时先复现、跑相关回归；合入 main 或发布前覆盖三层测试。用户明确接受的例外应连同原始失败保留记录。

| 范围 | 命令 |
| --- | --- |
| 默认 unit | `.venv/bin/python -m pytest -p no:cacheprovider` |
| 纯 UI | `.venv/bin/python -m pytest -p no:cacheprovider -m 'ui and not integration'` |
| integration | `.venv/bin/python -m pytest -p no:cacheprovider -m integration` |
| 全量 | `.venv/bin/python -m pytest -p no:cacheprovider -m ''` |

`pyproject.toml` 默认过滤 UI 和 integration，因此普通 `pytest` 不能称为全量。已安装 xdist 时，可为 unit 加 `-n auto`，UI 用适量 worker 和 `--dist loadfile`；真实媒体并行度需考虑机器容量。

浏览器测试标记 `ui`，调用 ffmpeg/ffprobe 或处理真实媒体的测试标记 `integration`。测试守卫拒绝将重型行为混进 unit。分层探测工具在 `devtools/test_layers/`，仅在需要重新核对标记时使用；探测结果留在仓库外。

测试使用独立临时数据。真实业务验收另记固定提交、有效配置、输入来源、业务结果、播放和远端回执；单元测试、队列 done 或页面显示 completed 不能代替整条业务验收。

## 修改与协作

- 在功能分支小步修改和提交；可 push 供 review，不主动开 PR。合并或推送 main 需要用户明确指令，不 force push。
- 保留 `#helpNav` 隐藏。当前未完成事项统一查[项目状态](../docs/status.md)，不把旧计划自动视作新任务。
- 配置沿用 `util.load_config()` 与现有 JSON 路径。新增模型字段先查真实契约或样本，不推测 API。
- 开发、测试与生产数据分离，不复制账号库、任务状态或交付回执来伪装结果。多机真实原片按各自原地址下载，保持约定配置一致。
- 并发验收保持 FBBM 禁用、OSS 仅测试命名空间、飞书/CF-Ads 拦截、Metabase 只读、SAM CPU；其他例外按明确授权记录。
- 文档、代码与配置变更完成后检查 `git diff --check`。生产发布还须记录版本、全量结果、健康、回退和配置差异。

## 兼容 CLI 与历史资料

命令行入口仍为 `bin/exactcut`，先使用 `./bin/exactcut --help` 核对当前命令。历史 Excel 时间写法见[标准表说明](../docs/archive/source/docs/标准剪辑Excel_分秒毫秒填写说明.md)和[编导表转换记录](../docs/archive/source/docs/编导Excel转换规则.md)，不能据此把新版网页帧输入当作毫秒。

旧 Mac、Tunnel、串行制作、早期 CLI 发布及首版 README 已归档，不作为当前安装或上线步骤。
