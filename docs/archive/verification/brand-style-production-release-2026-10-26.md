# 水印参考图正式发布（2026-10-26）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

参考弹窗里的说明图原先是 864KB 的 PNG，打开后才开始下载。现改为同尺寸 720×1272 的渐进 JPEG，约 66KB。

- 版本：`us-east-20261026-brandjpg-27caf4f`
- 代码提交：`27caf4fb1d70716c33e4016403eebacd5df36087`
- 镜像：`sha256:df1ba5279df9885f57e73caf9c3c81cee436a5665ec69f051bb3511c81646705`
- 入口：https://autocut.creativefitting.ai/home/
- 上一版本：`us-east-20261026-originals-0029b0d`；数据 schema 仍为 1。
- 正式切换：2026-10-26 22:11（Asia/Shanghai）。

Linux 隔离回归 1503 passed、139 skipped、10 warnings，336.23 秒。测试日志 SHA256：`668f20bade79325cdaab961f936d30642800a714c932886311691a756a42d0c7`。切换前 allowed=true、active_tasks=[]、phase_blockers=[]。切换后公网与本机 healthz 均为 production、新 release、worker_alive=true、maintenance=false。容器内说明图为 67612 字节的渐进 JPEG。

回滚：`linux-ops --root /srv/exactcut rollback us-east-20261026-originals-0029b0d`。
