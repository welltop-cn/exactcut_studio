# 素材版本与 OSS 发布验收记录

状态：2026-10-11 已发布，OSS 媒体分流已启用；素材版本管理仅纳管一个独立合成测试项目，尚未全量启用。真实 FBBM 交付未执行，新版交付保持关闭。

## 发布与运行状态

- release：`us-east-20261011-media-ee8267f`；源代码提交：`ee8267f6ec1485745c02dfe89df41a34ed4308b3`。
- 实际镜像：`sha256:800f781c8e82e48d6087de0d1eb112e0779a4d3e07234fef49a362fdcde7038d`。
- 北京时间 2026-10-11 01:52:49 经既有 linux-ops 发布，未直接修改存档 release。
- OSS 开启；`material_lifecycle.enabled=false`、`delivery_enabled=false`。通过显式 enroll 仅纳管下述合成项目，普通项目继续既有制作流程。
- 最终生产 `/healthz`：status=ok、worker_alive=true、maintenance=false；发布管理器 active_tasks=[]、phase_blockers=[]。
- 真实生产只读 `check-switch us-east-20261010-latest-0c6ffbe` 按预期拒绝：存在素材版本登记，旧镜像不支持协议。没有实际执行生产回退或数据库恢复。

## 数据保护与 OSS 范围

- 发布前在服务器私有目录 `/srv/exactcut/history/before-media-20261010T170043Z` 备份 483 个 JSON/JSONL 元数据文件及运行配置、发布记录、发布管理器；账号数据库使用 SQLite backup API。目录 700、文件 600。
- 桶 `aigc-us02-content`，应用媒体前缀 `exactcut-previews/production/`，新媒体对象显式 private。没有删除 OSS 对象，没有修改桶 ACL、CORS 或生命周期，没有物理清理旧成功版本。
- 内容按哈希键保存；重新制作成功后原子更新当前版本引用，旧成片保留。删除素材/项目是逻辑删除，恢复沿用原身份。
- 历史补传完成：20 个项目、40 份正式成片与 40 份预览，80 份媒体 ready，合计 4,356,770,378 字节。没有重新制作；已有有效回执可重用。
- 再次实际运行补传，86 份 ready（含普通流程验收项目的 6 份），1 个纳管项目跳过、失败 0；对上传入口计数并拦截，新增上传尝试为 0。
- 两个合成项目限定前缀的实际 OSS 列举及 ACL 审计：14 个对象全部 private，14 个回执键全部存在。对象清单保留，未清理。
- 桶生命周期只读查询为 NoSuchLifecycle；不能据此证明其他应用没有独立清理任务。

## 代码与审查

Grok 完成方案及代码多轮复审，最终对上述灰度范围**有条件通过**；独立规格和质量复审通过。见 [Grok 记录](material-lifecycle-grok-review-2026-10-11.md)。

主要实现：稳定项目/素材身份、不可变候选输入和成片版本、执行权围栏、匹配的 master/preview 双回执与原子 current、失败保留旧版本、逻辑删除恢复、复制新身份、版本绑定的交付历史及 unknown 不自动重放。渲染完成自动上传 OSS，预览签名续期，正式成片重定向 OSS 下载；交付实现支持服务端复制，真实 FBBM 验收仍未执行。

最终源代码测试：

| 环境 | 结果 |
|---|---|
| 本机独立全量 | 934 passed，138.23 秒 |
| 真实服务器最终 Linux 构建容器 | 932 passed、2 skipped，181.65 秒，exit 0 |
| JavaScript 语法及 git diff 检查 | 通过 |
| 独立 Docker 发布/回退/恢复 | 3 项通过 |
| 真实 Linux 进程故障实验 | 5 项通过 |

Docker 演练：pending 登记拒绝旧协议；兼容 A→B→A 保留状态；维护态显式恢复兼容版本。A/B 是同一受测镜像的不同发布封装，不代表生产已执行回退。独立实例未挂载生产数据/凭据，容器已停止移除。

进程实验覆盖：登记落盘后 SIGKILL 恢复、真实 flock/CAS 竞争、freeze 后 current 前 SIGKILL 与迟到围栏、current 后证据前 SIGKILL 的幂等完成、真实子进程完成后暂停窗口与显式仅补传重试。最后一项核验重试前后媒体 inode/mtime/SHA 不变。使用最终 Linux 镜像和真实子进程/锁，媒体为测试字节且 OSS 关闭；不是全调度循环的完整视频故障测试。

## 真实生产灰度：17 项通过

[纳管验收项目：999811 · 素材版本验收-合成视频-勿交付](https://autocut.creativefitting.ai/home/#27c933d449d6420384b07964d7f7ab15)

普通流程复制项目：`a7246381f0a44e0691865f13311aae2d`。专用账号和独立 15 秒合成源，无业务视频进入隔离环境。

已实际通过：多素材创建/纳管、跨账号拒绝、候选排队、真实 FFmpeg 制作与 master/preview 自动上传及 SHA 关联、并发保存一个 200 一个 409、重复新增幂等、素材逻辑删除后拒绝新签名与同 ID 恢复、重新制作期间保留旧 current、暂停/恢复同项目、仅修改素材更新版本且旧文件保留、复制分配新身份、新版交付关闭时拒绝入队、项目删除/恢复、删除全部素材再恢复、fetch/render 正在执行时删除候选并阻止迟到替换、普通流程继续制作且三素材自动上传、7 个内部执行项目对用户隐藏且直接访问拒绝。

最终纳管项目 3 份当前成片可预览，无活动任务。最后的取消实验有意保留上一次成功成片；草稿中新修改不等于已经成功发布的新成片。FBBM 外部调用数为 0。

## 真实 OSS 与浏览器

- 三份预览真实签名 Range 均 206，匿名访问均 403，签名有效期 900 秒。
- 真实 1 秒签名到期后 403，应用重新签名后 206。
- 三份正式成片通过应用下载入口 302 到 OSS，完整下载 SHA 匹配当前版本；服务器到 OSS 完整下载约 0.66–0.82 秒。
- 实际 Chrome 下载文件 2,782,151 字节，SHA `02301798ee16ab89acf25d45b43b27b1d5fbebf5948c1a7c168e4adf90b5a361`，与当前 master 一致。
- Chrome 原生 video 三份均 readyState=4、完整缓冲、无错误。通过客户端模拟临近过期触发真实续签及重新加载，暂停状态和 2 秒播放位置保留。此模拟与上述实际过期签名测试分开记录。

### 视频预加载期间切换与刷新

同一已登录 admin Chrome、同一批已完成项目，分别 10 次切项目、5 次真实 location.reload()。共 20 次切换、10 次刷新，15 次预加载采样全部观察到真实 video 活跃传输。

| 指标（毫秒） | 无预加载：中位数 / 最大值 | OSS 预加载：中位数 / 最大值 |
|---|---:|---:|
| 详情 API 首字节 | 259.95 / 305.10 | 260.20 / 270.10 |
| 页面 HTML 首字节 | 248.40 / 428.30 | 250.00 / 255.30 |
| openBatch 完成 | 345.35 / 392.70 | 378.05 / 885.40 |

每次检查项目 ID 一致、已完成项目默认执行进度、旧 video 暂停并清除 src、新项目进入预览前没有视频源。草稿仍按既有契约默认编辑页。测试结束恢复原项目 `452471dced424bec8bd5b668e641402d`，原 dirty=false，未关闭用户标签。

边界：连接的 Chrome 标签处于后台，requestAnimationFrame 暂停，因此在选择预览后显式调用同一生产 loadPreview 方法启动原生视频。以上是实际 OSS 传输与刷新并发测试，不是 Mac mini 前台自动预加载或键盘 Command+R 的直接验收。性能测试时服务器还在历史补传，不能当作空闲网络基准。样本中未再出现原先数十秒 HTML/API 等待；未证明原链路具体哪一层阻塞。

## 尚未执行及全量开放门槛

- 真实 FBBM 接收、OSS 复制交付及外部未知响应恢复未执行：没有确认可产生测试素材的目标；新版交付及全量自动纳管保持关闭。
- 验收矩阵中的每个故障窗口、异常历史文件、外部交付竞态没有逐项在真实 OSS/完整调度器重演；相关回归使用隔离 fixtures/stub。不能将全量单测通过等同于所有线上场景通过。
- Mac mini 用户日常前台交互、自动预加载和键盘刷新待用户验收。
- 已纳管项目独立 Hook 预览及草稿源预取有保护性限制；全量开放前需核对相应产品行为。
- 损坏的纳管权威状态 fail-closed，可阻止名称分配及共享调度 tick，需修复后继续，不能跳过未知预留。pending 登记恢复要求显式 enroll()，已有登记的 HTTP enroll 不代替此恢复。HTTP 纳管外层写屏障仍可能使其他写请求等待冷迁移。
- 保留旧文件/对象意味着磁盘和 OSS 占用会增长；当前未启用任何物理清理。

## 证据位置

服务器：`/srv/exactcut/acceptance/media-20261011/`：`acceptance-report.json`、`media-network-result.json`、`historical-backfill.jsonl`、`backfill-reuse-result.json`、`fixture-oss-object-audit.json`、`linux-worker-fault-result-v2.json`。账号凭据仅在服务器私有文件，未进入报告或代码。

Docker 演练：`/srv/exactcut/acceptance/release-media-us-east-20261011-media-ee8267f/exercise-result.json`。

本机私有证据：`~/.codex/artifacts/exactcut-oss-grok-20261011/`，含上述报告副本及 `browser-performance-result.json`、`browser-performance-summary.json`、`browser-download-result.json`、`browser-renewal-result.json`、`grey-project-progress.png`。报告不包含 Cookie、密钥或签名 URL 查询参数。

[验收矩阵](material-lifecycle-server-acceptance-plan.md)保留原计划，执行结果以本记录的环境、范围与限制为准。
