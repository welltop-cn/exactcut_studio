# 最新版本发布与最近 10 个提交线上验收

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 基线与发布

验收范围固定为 `git log -10` 在合并提交 `3666d14e95d3b9d4326c8f33d5eba857ee34bcbd` 上的结果，包含合并及文档提交。主工作区最新功能与已上线 admin/切换修复已合并，无冲突，发布前工作区干净。

2026-10-10 23:12（上海），经 root 和现有 linux-ops 发布 `us-east-20261010-latest-3666d14`。镜像 `sha256:826f9e3c8e62df4e6b2e8fc9cea60cfa21f8e4f143487f1b4d5069d664324277`。

本地全量 853 passed in 130.54s；Linux 发布镜像全量 851 passed / 2 skipped。静态 JS 与 diff 检查通过。公网 healthz 确认 release 匹配、status=ok、maintenance=false、worker_alive=true。

## 按提交验收

| 提交 | 改动 | 验收方式与结果 |
|---|---|---|
| 3666d14 | 合并最新功能与线上修复 | 镜像提交与 release 一致；admin/producer 页面刷新后可用，线上脚本 3acebf133568fe693ff9 |
| edc7c49 | 侧栏名称换行、元信息分行 | 线上实际项目名 computed white-space=normal、overflow-wrap=anywhere；scrollWidth=clientWidth=191；作者与素材数/日期分开显示 |
| b30387f | 进度默认页发布记录 | 对照原版本号、提交与既有验收记录；文档无运行时行为 |
| f313c22 | 渲染前切到执行进度 | 线上从 review 切项目 561 ms，结果 tab=progress；旧视频 paused 且 src 移除；新项目预览请求 0；快速 A→B→A 后 selected/current/hash 一致，openingBatchId=null |
| 8ffea34 | 素材独立历史、合并重复空闲事件 | 线上真实项目 34 条原始事件：项目区 8 条，两素材各 12 条，归属检查通过；展开状态刷新后保留。线上已加载函数的纯内存边界输入验证：重复空闲归为 2 条，素材记录 5 条，两个失败均保留，转义通过，输入不变 |
| efe52d5 | admin 列表发布记录 | 对照原版本号、提交和测量范围；文档无运行时行为 |
| 0e0b9ac | 全局队列限管理员 | 线上 admin GET=200 且队列可见；producer GET=403、队列隐藏，刷新页面后无自动 queue 请求 |
| 027c0c4 | 精简侧栏、并行刷新与锁范围 | 线上 admin sidebar=200、22 项目、6252 字节、不含 events；producer=200、仅本人 1 项目。锁释放与轮询不等待侧栏由同镜像隔离回归验证 |
| 9108b5c | 显式启用开发环境 AI | 在生产容器只读检查 environment=production、ai_flag=true、require_ai=allowed。development/test/staging 的开关矩阵由 Linux 隔离回归验证；不在生产修改环境或发起真实 AI 调用 |
| 229647e | 下载完成后隐藏旧观测 | 线上实际完成项目 download_complete=true，executionObservation.hidden=true |

## 测试边界

- 浏览器是已有线上 admin/producer 登录上下文，均先确认 dirty=false 再刷新。admin 最终恢复项目 452471dced424bec8bd5b668e641402d。
- 未创建制作任务、未上传、未修改生产账户或任务状态。唯一项目操作为保存当前选择；纯函数测试输入只在浏览器内存，不写入项目。
- Chrome admin 标签位于后台，0 媒体请求不能单独证明前台 Mac mini 体验。预览可见时预下载由已通过的回归覆盖，代码保留；本次不声称消除了共享媒体/API 链路的所有延迟。
- 实际线上页面/API 检查、线上函数内存边界测试、隔离环境测试分别列出；开发环境专属行为没有冒充生产实测。

未发现本轮验收阻断项。
