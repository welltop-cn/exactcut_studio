# 编辑与制作时间线修复发布

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

- 用户明确批准发布后执行，2026-10-11T02:31:01Z 切换成功。
- 版本：us-east-20261011-editor-828ea5b；提交：828ea5b938d220c2e76a3549d7ee13ae30f31af8。
- 镜像：sha256:aaa32302d6722dd7cce1712f35a3f5c6352dff6b4dfd558c48d31beb8504d229。
- 本地完整 1015 项通过；Linux 1009 passed / 6 skipped，193.21 秒。跳过 Docker CLI 与浏览器依赖相关检查，本地浏览器回归已通过。
- 发布门槛 active_tasks=[]、phase_blockers=[]、allowed=true。备份 /srv/exactcut/history/before-soak-20261011T022951Z，1131 份元数据及 SQLite 在线备份。
- 发布后 healthz=ok、worker_alive=true、maintenance=false。200 个源码文件与本地 SHA256 一致；运行容器镜像与发布记录一致。
- 真实浏览器：用户原复制项目显示 8 行逐段剪辑字段、无文本时间线、无“旧成片保留”提示。该项目已被归档，所以输入禁用符合当前状态，没有为验收擅自恢复。
- 未归档原项目 39b4f695712a4c44ba9a0af940e821d2 点击“修改后重新制作”：仍为同一项目，进入 editor、8 行片段、输入启用、dirty=false、busy=0，没有启动任务。其两素材 production_events 各 12 条，首事件分别为本轮 01:50:42Z / 01:54:05Z，没有嵌套 technical-events 面板。
- 一次浏览器验收直接改变 URL hash，没有触发应用选项目逻辑，因此仍显示原归档项目，按钮不可见；改用已观察到的侧栏入口后验收通过。未重复生产写操作。
- 浏览器所有权已释放，标签保留。未提交 FBBM、未新建验收用户、未新建项目、未启动新制作。

此前 production-timeline-local 和 copied-project-editor-local 报告的“未发布”是当时状态，本记录更新为已发布。Grok 对两批修复均已完成只读复审，无阻断问题。
