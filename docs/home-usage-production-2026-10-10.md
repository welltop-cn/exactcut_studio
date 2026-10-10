# /home 与管理员用量生产发布记录

2026-10-10，接续 Pi session `01a12099-6e35-708c-8b0c-a30a49e2281f`。

- 原候选 home 未发布：769 passed / 7 skipped / 1 failed，新增前端验收因验证镜像缺 Node 失败。
- 修复提交 `7bd9cdf30668aaa1288fdd634af21173d66eb63b`：仅在验证镜像安装 nodejs。保留失败版本，重新构建新版本。
- 发布版本 `us-east-20261010-home2`：Linux 全量 **775 passed / 2 skipped，131.14 秒，exit 0**。镜像 `sha256:4157b3dce7b47d2a332389252743ceb399200cc3a0679f179a7c88bca473e11f`。
- 07:53 UTC 经发布管理器从 `us-east-20261010-ui` 切换；切换前无活动任务或阶段阻塞，schema 保持 1。
- runtime 的 web.base_path=/home；nginx 代理 /home/，根地址跳转 /home/，其他路径 404。nginx -t 通过后 reload。
- 配置备份：服务器 `/srv/exactcut/backups/home-cutover-20261010/`，包含 runtime.json、站点和 snippet。恢复旧入口须同时恢复配置并通过发布管理器回滚代码；代码回滚不会自动恢复配置。

线上验收：

- 公网 /home/login=200、/exactcut/=404。
- 经 nginx：根地址 302 至 /home/，/home 301 至 /home/，旧前缀及其 app.js 均 404。
- 实际管理员登录 200，Cookie Path=/home/；工作台 base href、新用量入口、app.js、helper.js 内容验证通过。验收登录会话已退出。
- 匿名用量接口 401；管理员 200，返回 totals/projects/users/shared/generated_at。当前后三张表为空，数据根逻辑大小 32948 字节。
- admin、producer06 账户保留，未清理用户数据。
- 最终健康状态：production、release=us-east-20261010-home2、status=ok、worker_alive=true、maintenance=false。
- 制作人 403、UI 隐藏、排序和只读交互由完整回归覆盖；没有修改真实制作人密码做线上登录。

既有用户需在新入口重新登录。
