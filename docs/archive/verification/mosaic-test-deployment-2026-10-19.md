# 剪切打码 · 线上测试部署验收（2026-10-19）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 发布

- 用户明确授权：更新 Docker 配置、提交代码、部署线上测试实例；未授权正式发布，本轮未操作正式容器。
- 源：feat/mosaic-gemini，75efc57，构建时干净；包含 main 的全部提交。
- 包：exactcut-mosaic-75efc57.tar.gz，SHA256 `598604715c8eae2dd1a21f6b5b4dc36acb868e5a5b325143bc72e01c1b02df6e`。
- 镜像：exactcut-test:mosaic-75efc57，Python 3.12，SAM 2 CPU 可选依赖已启用；torch 2.7.1+cpu，CUDA=None。
- 入口：http://8.221.122.75/test/；release=test-mosaic-75efc57，health ok，worker_alive=true。账号沿用原测试账号。
- 运行容器 exactcut-test，6 CPU / 6 GiB / 256 PID。模型挂载 /models:ro，应用源码与配置只读，data 原样保留。
- 集中 runtime.json 仅新增 cutting.tracking，device=cpu，模型与源码固定版本及哈希见部署手册。原 AI、OSS test 前缀及用户既有交付设置未改。未提交或上传任何素材到外部交付流程。

## 验证证据

- 本地完整套件：1546 passed（355.53 秒），10 个既有 fork 弃用警告。
- 同镜像 Linux 隔离回归：mosaic_render、mosaic_tracking、mosaic_effects、linux_copy 共37 passed（18.62秒）；只读源码使 pytest 无法写缓存，1个缓存警告。未挂载任何业务数据或凭据，网络关闭。
- 同镜像、同6CPU/6GiB限额真实 SAM：8帧合成移动目标，8帧均有有效遮罩；CPU跟踪12.15秒，总跟踪及FFmpeg合成13.65秒；源哈希不变，输出帧数/分辨率/帧率一致。该短样本证明运行链路，不代表长片性能或真实人物跟踪质量。
- 服务器证据：/srv/exactcut-test/acceptance/mosaic-75efc57/（acceptance.json、run.log、pytest.log、source/output视频及mask）。
- 公网Chrome沿用正常登录态，项目列表加载、原有项目进入、剪切打码弹框初始化、1080×1920原片播放（readyState=3、currentTime>8秒、error=null）、添加片段面板及返回均正常。未保存新增片段，未替换原成片。
- 页面验证项目：http://8.221.122.75/test/#b3407a1ca44a43508e4619a629edd511。
- 原 producing 状态项目1c58e8fe627b423bbc4906f933bb0226实际control.paused=true、archived=true；保持不变，启动后全局队列0条。
- 正式版本仍 us-east-20261015-observer-f147248，StartedAt仍2026-10-15T10:38:18.966868767Z，health ok、worker_alive=true；未重启。

## 回滚与范围

- 原测试代码：/srv/exactcut-test/code-mosaic-75efc57.previous。
- 原runtime/base-image/recreate脚本：/srv/exactcut-test/deployments/mosaic-75efc57-backup。
- 发布记录：/srv/exactcut-test/deployments/mosaic-75efc57.json。自动切换脚本对健康失败仅回滚测试代码、镜像与配置，不回滚数据。
- 动态跟踪使用CPU，明显慢于GPU场景，不能用8帧测试承诺长片延迟。失跟按用户约定跳过，不整帧打码。
- 仅部署线上测试实例，正式生产版本不变。
