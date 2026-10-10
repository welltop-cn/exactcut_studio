# 默认 /home 与管理员只读用量 · 实施及验收

2026-10-10；分支 `concurrency-refactor`；schema 保持 1。仅本机开发与临时数据根验证，未部署、未连接或修改生产。

## 行为与统计口径

- `source/config/default.json`、`production.json` 的 `web.base_path` 均为 `/home`。沿用现有路径归一化、登录跳转、`<base>` 与 Cookie 实现，没有旧网页前缀的兼容或跳转。Cookie Path 为 `/home/`；部署方切换既有 runtime/nginx 后，用户需要重新登录一次。
- `linux_ops.nginx_config(..., base_path='/home')` 回归确认输出 `location = /home` 的 301 `/home/` 及 `location /home/` 的保留前缀代理，无需修改 nginx 生成器。本次不执行 nginx 操作。
- `GET /home/api/usage`（应用内部 `/api/usage`）先认证、再 `is_admin()`，匿名 401、制作人 403、管理员 200。无 POST 用量接口。既有 `/api/storage` 和清理流程未改。
- 项目按批次目录全部普通文件的逻辑字节数求和；现行目录为 `.web-state/batches/<id>/`，同时覆盖规范中的 `batches/<id>/`。`access.json.owner` 决定归属，仅文件缺失时沿用 `accounts.owner()` 回退 admin。项目名依次使用 display_name、drama_title、filename、目录名；素材数取 schema 1 草稿 rows 数；状态取持久 runtime.status。
- 按剧共享：`sources/<剧>/` 与 `projects/<剧>/source/videos/` 合并为 `sources_bytes`（正片）；`projects/<剧>/cache/` 为 `cache_bytes`；`projects/<剧>/data/episodes/` 为 `episodes_bytes`。`referenced_by` 为引用该剧的批次 owner 去重排序；含已归档批次。共享字节不加入用户私有合计。无引用缓存仍展示；有引用但尚无缓存的剧展示零占用。
- 数据根合计包含全部普通文件（含账户、任务、反馈等管理文件），因此不要求等于项目与共享两类之和。跳过符号链接和非普通文件，不跟随链接到数据根外。这里统计文件逻辑大小，不是文件系统分配块或硬链接去重后的 `du` 值；扫描是运行中文件的近似快照。
- 管理员“用量”对话框展示数据根合计、磁盘剩余/总量、生成时间及三张表；各表按占用降序，共享按三类之和排序。仅关闭按钮，无删除、清理入口；输出文本转义，支持空表。

## 扫描与错误处理

`usage_report(root)` 一次 `os.walk` 分配所有文件字节，不为每个项目重复遍历。返回 `generated_at`（现有 `now_iso()` 格式），进程内缓存 45 秒。持扫描专用非阻塞锁，不持工作流全局锁；同一时刻最多一次扫描，忙请求立即返回 503，其余 API 可继续读取。

扫描在目录、文件、批次元数据及完成处检查 10 秒预算；超预算、权限或损坏元数据返回 503，不发布部分合计。失败也缓存 45 秒，防止反复触发大目录扫描；冷却结束可重试。预算为文件系统调用之间的协作检查，不能强制中断操作系统中阻塞的单次 I/O；不启动遗留后台扫描线程。正常返回后缓存从完成时起计时。

## TDD 与验证

- 第一轮：9 failed / 43 passed，缺少 usage_report、用量 UI、管理员接口和新默认值。HTTP 测试夹具先纠正为实际 `accounts.manage()` 契约，随后单独观察制作人 404（预期 403）的功能缺失失败。
- 实现后专项：58 passed，含路径、Cookie、视频 Range、nginx、环境隔离及现有存储清理回归。
- 补异常元数据、同用户多批次、繁忙 HTTP 的回归：2 failed / 10 passed；复现合法 JSON 数组/null 导致连接断开。加入对象类型校验后 12 passed；异常返回 503，忙请求不阻塞 `/api/me`。
- Node 执行实际前端初始化、渲染和按钮处理器，验证管理员可见/制作人隐藏、三表排序、文本转义、空态及仅 GET 请求。Chrome 本地临时服务器复核三表显示、管理员可见、制作人隐藏及 403；无业务外部请求。
- macOS 15.8.1 / Python 3.12.12；全量测试使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`，清除继承的 EXACTCUT/OPENAI/ALIYUN/FEISHU/CF_ADS/GEMINI/GOOGLE_API 环境，独立 TemporaryDirectory 数据根，不固定 SOURCE_ROOT。

最终本机全量：**777 passed / 108.78s，退出 0**。从 `d53cdc20c7682159d4391cdb8771d10a476467d4` 经 `git clone --no-hardlinks` 创建独立干净 clone：**777 passed / 111.33s，退出 0**，无跳过；导入确实来自 clone/source，测试前后 `git status --porcelain` 均为空。两次验证的 source tree 均为 `78bee238a2184ff6daa4b6896e2d94e8ca1c02ed`。验证后仅补记本文并 amend 提交，source tree 不变；`git diff --check` 通过。

复现命令：在 `source/` 下执行下列脚本；clone 验证还需显式将 PYTHONPATH 指向 clone/source、核对 exactcut 导入路径及测试前后 Git 状态为空。

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 - <<'PY'
import os, subprocess, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='home-usage-tests-') as root:
    prefixes = ('EXACTCUT_', 'OPENAI_', 'ALIYUN_', 'FEISHU_', 'CF_ADS_', 'GEMINI_', 'GOOGLE_API_')
    env = {k: v for k, v in os.environ.items() if not k.startswith(prefixes)}
    env.update(EXACTCUT_DATA_ROOT=root, EXACTCUT_ENVIRONMENT='development', PYTHONPATH=str(Path.cwd()))
    raise SystemExit(subprocess.call([sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q'], env=env))
PY
```

原有 `docs/server-sizing.md` 改动不纳入本次提交；`docs/现行架构与处理流程.md` 只暂存本次入口表述，原有段落保持未暂存。原有未跟踪东京下线文档仅将历史 URL 改写为“当时配置的入口，由部署方维护”，保持未跟踪。代码包名、命令及 `/srv/exactcut` 等文件系统路径不是网页入口，不改名。
