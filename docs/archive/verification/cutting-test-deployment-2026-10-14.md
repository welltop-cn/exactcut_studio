# 剪切功能：独立服务器测试实例

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

- 已部署代码提交：ae8acd3。代码来自git archive的已提交白名单；未提交的历史演示与验收资料未打包。
- 包SHA256：b0eb1c6630fb900f8630a049fc1567d94376b3b116d71b64d27244d7985b4c41。
- 地址：http://8.221.122.75/test/，容器exactcut-test，release=test-cut-ae8acd3。保留原账号、项目、数据及暂停/归档状态。
- cutting.enabled=true；cutting.scan采用完整默认识别配置并enabled=true（集中配置只做section浅合并，不能仅覆盖scan.enabled）。识别凭据独立只读挂载，不写入Git或记录。
- OSS namespace=test，前缀exactcut-previews/test；delivery_enabled=false，publish/external_writes仍为0。正式服务未改动。
- 本地完整回归通过（6条既有fork警告）；环境开关API/识别专项20项通过，覆盖production显式启用、默认关闭、缺少识别配置时关闭。
- 候选使用原测试镜像隔离验证imports、开关及凭据读取后切换，未更换依赖镜像。
- 服务器实际登录/API验收项目fcdbc546fd914ebca1f83d5f0e85e58d：113帧删除前3帧生成110帧，ready，MP4 Range返回206，项目当前hash不变。未执行项目替换或FBBM上传。
- Gemini真实识别completed，无错误，0条建议；不代表素材审核通过。生成与识别同时抢占处理资源时返回429，生成结束后识别正常启动完成。
- 公网healthz正常，worker_alive=true。正式release=us-east-20261014-main-d7d8576、StartedAt=2026-10-14T02:36:41.924871886Z，更新前后相同。
- 回滚代码：/srv/exactcut-test/code-ae8acd3.previous；旧runtime.json/recreate.sh：/srv/exactcut-test/backups/cutting-ae8acd3/。回滚只切换测试代码及配置，保留data，使用测试recreate脚本重建exactcut-test。
- 服务器验收证据：/srv/exactcut-test/deployments/cut-ae8acd3-acceptance.json。

仅部署测试实例，未发布正式服务。页面浏览器端实际交互由用户后续验收；本次服务器验收使用鉴权HTTP接口。
