# 播放诊断与取消竞态正式发布（2026-10-27）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

用户明确授权把今天修改发布真实实例。通过既有linux-ops构建、校验和publish完成；测试实例按先前要求保持停止。

- 正式版本：us-east-20261027-playback-a5f7633。
- 发布提交：a5f7633d2f791c29cf905973b63da1246eab71bf；应用代码与测试验收62c42b3完全一致，额外提交仅记录验收文档。
- 镜像：sha256:0a69af7d02c80ba4930f6ffbf5c4896e6f2aca28314bba8824a367887de21f31。
- 前一版本：us-east-20261026-brandjpg-27caf4f。
- 发布时间：2026-10-27 19:41（Asia/Shanghai）。入口：https://autocut.creativefitting.ai/home/ 。

## 范围和验证

相对正式27caf4f，仅新增编辑器媒体诊断、过滤被主动取消的旧play拒绝、成功播放后清除对应错误、暂停时同步复位按钮及相关测试。未改依赖、数据schema、视频Range、计算锁、抽帧锁或业务配置。

- 本地相同应用代码完整1672项通过、无跳过；Pi deepseek-flash high复审通过。真实视频测试实例验收见[记录](playback-test-acceptance-2026-10-27.md)。
- Linux发布管理器隔离回归1505 passed、167 skipped、10 warnings，338.21秒，退出码0。跳过项不计通过（Linux无Playwright浏览器环境；本地已覆盖），测试日志SHA256=4437a72429230c1a695780dcd2d5c0ac9eb9f47b2e8a2fdf13b8fc1bfc8027c2。
- 独立root所有干净Git仓库构建，原工作树两份无关未跟踪文档未打包。bundle SHA256=3f1604e2c526dc76f56389417b5cfe4e0df04375360a085c09d73c0d51e13360。
- 发布前check-switch：allowed=true、active_tasks=[]、phase_blockers=[]。管理器在维护锁内再次核查并切换，未强制结束任务。
- 切换后本机、公网healthz均status=ok、新release、worker_alive=true、maintenance=false。
- Chrome沿用已有登录打开真实工作台，项目正常载入、连接显示自动同步，cutting.js/mosaic.js/diagnostics.js版本分别为ebb5f79fcb696eef6b79、98243974314c317562ff、480088fcde06ab0a9a09，与本地和容器文件SHA256前缀一致。
- runtime.json、deployment.json、production.env发布前后SHA256校验全部一致。不输出配置内容。普通代码发布保留业务数据，未执行素材制作、替换或外部发布验证；没有向生产注入伪造播放异常。
- 测试容器exactcut-test仍为exited，账号/配置/数据保留。完成后关闭自己的正式验证标签页，未操作用户其他标签页。

## 回滚和证据

服务器构建/测试封存：/srv/exactcut/releases/us-east-20261027-playback-a5f7633/。

发布档案：/srv/exactcut/preparation/release-playback-a5f7633/（build-result.log、check-switch.json、publish.log、配置哈希及发布前后健康记录）。

回滚命令：

```sh
/srv/exactcut/bin/linux-ops --root /srv/exactcut rollback us-east-20261026-brandjpg-27caf4f
```

回滚仍需通过任务排空与兼容性门禁，不恢复旧数据库或删除业务数据。发布管理器已具备切换健康失败自动恢复上一镜像机制，本轮发布成功，未执行回滚。已有浏览器页面需刷新以加载新脚本；历史错误无法由新增日志追溯补齐。
