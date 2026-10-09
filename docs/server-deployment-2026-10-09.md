# 东京服务器上线记录（2026-10-09）

**最新版本：**`linux-20261009-3`已上线，增加按账号首次创建默认项目及恢复上次项目，移除网页Excel入口。规则及最新验收见[首次进入与项目恢复](workspace-startup.md)。下文保留初次公网部署版本与媒体验收证据。

公网地址：**http://124.156.228.202/exactcut/**。沿用现有nginx的80端口，以路径区分服务；`/exactcut`自动301到`/exactcut/`。原有`/paypal_dispute/`、`/domain-watch/`在变更前后均从公网验证为200。

## 正式版本

- 版本：`linux-20261009-2`，上一版本`linux-20261009-1`。
- 干净工程提交：`59851988c9a7a69650d05f478d7cfac5d8290b5f`。
- 固定镜像：`sha256:af56410ab15d8290485664e6c1f15a9ef17016ecc164919a51eee6266cc1d19c`。
- 发布管理器构建、测试、切换和健康检查成功；schema保持1，维护状态已解除，worker正常。
- 宿主全量回归：326passed/1skipped，42.22秒；服务器Linux容器全量回归：323passed/4skipped，186.78秒。容器跳过Docker CLI/Node相关检查，宿主覆盖；宿主跳过的Linux进程组检查在容器覆盖。
- 下载、AI、飞书/FBBM发布及外部写入开关已启用。首次正式实例为空白业务库，不复制本地开发批次或账号。

原工作区已有的未提交改动保留。发布使用服务器`preparation/engineering`中的独立干净提交，未覆盖历史Mac生产服务。

## 配置与入口

部署根`/srv/exactcut`；唯一生产密钥文件`config/production.env`，运行配置`config/runtime.json`，机器参数`config/deployment.json`，均0600。密钥与数据不进入版本目录。应用限制仍为1CPU/2GiB；服务器规格建议见[服务器配置与实测](server-sizing.md)，本次没有修改编码preset或提高资源限制。

生产`runtime.json`增加`web.base_path=/exactcut`，`deployment.json`的`public_port=80`，后端仍为`127.0.0.1:18792`。开发默认`web.base_path`为空，继续用8793和独立数据，不加载生产配置。

nginx片段位于`/etc/nginx/snippets/exactcut-production.conf`，由`linux-ops nginx`生成并include到既有`sites-available/domain-watch`的server块中。nginx保留前缀代理给应用，应用处理路径、登录跳转与Cookie作用域；静态资源、模板、API、图片和视频链接均支持前缀。独立8792站点已撤掉，最终无8792监听。

安装前备份原nginx站点、机器配置与运维入口至`/srv/exactcut/preparation/path-activation-backup`，权限0700。语法检查后reload，等待新worker实际响应后再验收；reload返回不代表新配置已接管全部请求。失败时只恢复此次nginx改动，业务数据不回滚。

管理员用户名`admin`。初始化密码保存在服务器`data/.web-state/password`，0600；密码不写入本文或验收JSON。迁移和操作命令见[Linux部署与迁移](linux-migration.md)。

## 验收范围

从本地Mac通过80端口公网入口实际完成登录、会话Cookie检查、工作台HTML/JS、AI助手脚本、XLSX模板、功能开关、存储配置和空白批次查询；健康检查返回production、当前版本、worker_alive=true、maintenance=false。证据见[公网HTTP结果](server-public-http-2026-10-09.json)及[部署状态](server-deployment-state-2026-10-09.json)。视频Range、图片查询参数和根路径/多级路径的前端请求由隔离回归覆盖。

真实生产上下文的只读OSS下载与本地制作使用隔离目录`preparation/real-media-data`，不进入正式业务目录，未上传或发布。两集完整下载5.217/5.167MiB/s；输出101.5秒、1080×1920、30/1fps，四个时间线单元均匹配计划时长，AI语义选词与画面抽样定位执行成功，QC、轻量预览和缓存复用均通过。中文标题卡及Hook抽帧已目视检查。程序保留了“各候选位置均有遮挡风险”的提醒，仍需逐条预览确认。

视频验收使用此前已验收镜像，最新版本仅增加网页路径与HTTP兼容修改，渲染实现未改变。报告总耗时包含主动暂停及并行工作，不能当作独占机器的吞吐预测。证据见[真实视频验收](server-media-acceptance-2026-10-09.json)。验收容器完成后已自动删除。

外部服务鉴权与只读检查已通过；真实OSS上传、飞书写入和FBBM最终就绪仍需选择具体业务素材并经工作台预览确认。发布能力已启用，不代表已完成一次真实业务发布。

## 实际业务发布补充

2026-10-09 用户确认预览后，一条42秒业务成片已完成OSS上传、飞书记录创建与FBBM ready。发布耗时约129秒，其中上传15.767秒、FBBM处理113.099秒。此证据更新此前“写入尚未验收”的历史状态，仅覆盖该次业务素材，不代表所有输入和失败恢复场景均已覆盖。原始业务状态、记录ID和素材不写入Git。
