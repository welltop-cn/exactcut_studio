# 下载与制作队列进度

- 项目进度展示全局下载/制作负载，以及当前身份可见的任务详情。普通用户只能收到自己的项目名称、素材 ID 和阶段数据；管理员可收到完整活动队列。上传/poll 不进入此队列。
- `GET /api/queue` 要求登录。响应包含 `pools`（fetch/render 的 running、queued、paused、limit）、`tasks`（白名单字段）、`scope`（own/all）和秒级 Unix 时间 `updated_at`。展示按提交时间排序，但调度仍按用户轮转，不承诺 FIFO 位置或预计开始时间。
- 制作只受全局 render 上限约束。旧配置 `per_project` 不再参与调度或暂停恢复；下载、上传、poll 的配额与执行中的任务围栏保持原有规则。生产模板 render=3。
- 素材卡片展示当前阶段、用时、剪辑片段完成数。没有真实量化数据的编码阶段使用不定进度条；暂停和排队不显示运行进度动画。队列更新失败显示旧状态提示。
- 并行渲染阶段写入持久执行动态，相同阶段重复心跳不新增事件；批次汇总完成时仅追加一次制作结束。取消前后端事件截断；已经丢失的旧事件无法恢复。
- 回归覆盖：普通用户跨账号隔离、管理员全局可见、未登录 401、暂停计数、渲染全局上限、旧项目限制忽略、执行动态保留、真实阶段进度和排队/暂停/完成差异。浏览器夹具已核验桌面布局。

验证：无业务数据的源码副本运行完整回归，835 passed（126.71 秒）；JavaScript 语法与 git diff --check 通过。线上发布结果另行记录。

## 线上发布

- 2026-10-10 20:43（上海），通过 linux-ops 发布 `us-east-20261010-queue-progress`；代码 `ea4b5e7fc5f8cedcea1b1eed83b119d34117b8e4`。
- Linux 隔离回归：833 passed / 2 skipped；镜像 `sha256:ec384a8e1c743d52f46ad389b61cba21fb552a7421a9fe4abf4a2f4cdbc00a5b`。
- 实际配置：render=3，无 per_project 设置输出；fetch=2、upload=2、poll=4、max_active_children=11。旧 runtime 中遗留的 per_project 键已被新版本忽略，无需改其他配置。
- 使用现有管理员凭据进行本机 HTTP 只读验收，队列 API、含 taskQueue 的 HTML 和新 productionProgress 脚本均返回 200。对9名非管理员身份验证 owner 范围；当时生产队列为空，跨用户活动任务隔离由多用户夹具回归覆盖。
- 公网 `https://autocut.creativefitting.ai/home/healthz`：ok，release匹配，maintenance=false，worker_alive=true。未启动新的真实渲染或外部发布来验收界面。
