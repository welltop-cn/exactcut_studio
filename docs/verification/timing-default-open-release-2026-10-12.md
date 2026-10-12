# 时间解析校对默认展开发布（2026-10-12）

- 改动：时间解析校对初始展开；手动收起后正常时长刷新保持收起；时间错误或下载失败仍自动展开。保留原有交互及错误提醒行为。
- 代码提交：`4e703277b18f496b94c78d9f55a3e33d3b56d4bf`。本记录为发布后的文档补充。
- 先更新既有浏览器回归并确认默认展开断言失败，再实现修改；相关 10 项通过。JavaScript 语法检查及 `git diff --check` 通过。
- 本机隔离全量：1167 passed in 185.95s。Linux 隔离全量：1147 passed, 20 skipped in 226.08s；服务器缺少浏览器等依赖的跳过项已在本机验证。
- 由发布管理器构建、检查兼容性并切换，版本 `us-east-20261012-timing-open-4e70327`，2026-10-12 21:53:03（Asia/Shanghai）。发布前无活动任务和阶段阻塞。上一版本为 `us-east-20261012-clip-text-060923e`。
- 固定镜像：`sha256:30e984e7734b233a877483e5b8626ec0dacf47610e1df81792a3c71314ef1d4b`。
- 封存源码 229 个文件的 SHA256 与提交一致；运行镜像与发布记录一致；runtime.json 和 deployment.json 校验值未变。
- 正式登录后核对实际返回的 app.js，其 SHA256 与提交一致，包含默认展开标记；验收后退出登录，没有保存或修改制作项目。
- 公网 `https://autocut.creativefitting.ai/home/healthz` 确认为目标版本、production、status=ok、maintenance=false、worker_alive=true。
