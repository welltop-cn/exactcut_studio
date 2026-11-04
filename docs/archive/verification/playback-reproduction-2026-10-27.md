# 剪切打码播放失败：服务器测试实例复现

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

2026-10-27，用户授权复制正式项目配置和视频到独立测试实例；未授权正式发布，未修改正式服务。

## 样本和隔离

- 测试项目：`http://8.221.122.75/test/#671c71aa7db44409b1c8cda98220901e`。
- 来源项目：`359de48aa97146d18ca03fb3f4270f95`。复制两条素材配置、片尾图片、两条成片、引用的 EP1–EP5 独立视频；重新建立测试帧索引，项目 errors=[]。不复制账号、任务、发布回执或生产 OSS 引用。
- 第一条使用反馈时的原始版本，而非后续替换成片：149968786 字节、228.1 秒、6843 帧；SHA256 `a9eb33e8057579439096bfff5f608e2d9c316669057a7c098f1ae46ad9765dce`。复制后校验一致。
- 测试打码会话：`662f933611616ac80b051557d074a472`。
- 沿用测试代码 test-cache-655e0b4。mosaic.js / cutting.js 的 SHA256 与正式 27caf4f 完全相同。未部署本地日志补丁；只在本次浏览器页面临时观察原生媒体事件及 play Promise 结果，返回原 Promise，不改变播放决策。
- 测试容器仍限 6 CPU / 6 GiB / 256 PID。为本轮播放测试关闭 worker、AI、发布、外部写入、OSS 预览上传和 scan；保留已有测试项目和账号。
- 配置与启动器备份：`/srv/exactcut-test/backups/playback-repro-20261027/`；本轮容器重建使用 `/srv/exactcut-test/recreate-playback-repro.sh`。原 recreate.sh 保留历史开关，不应用它无意恢复后台/交付能力。

## 实际结果

Chrome 154/macOS，公网 IP HTTP 测试入口，真实前台点击和播放头操作：

1. 正常播放成功，play Promise resolved，播放时间前进、媒体 error=0。
2. 跳至 150 秒，加载后播放成功。
3. 快速操作：跳至第 6200 帧（206.668 秒），点播放，尚处 seeking/readyState=1 时再跳至第 3000 帧（100.002 秒）。第二次跳转调用 pause，play Promise 拒绝为 AbortError，界面出现“暂时无法播放，请重试”。这轮自动化的 play→下一次 seek 间隔约 9 ms，用于证明竞态路径，并不证明反馈人以同样速度操作。
4. 再点播放恢复成功。错误文案未清除，形成“正在播放但仍提示无法播放”的状态；保存证据时 currentTime=75.728、paused=false、readyState=4、media_error=0，文案仍在。
5. 对照：跳至 60 秒并点播放、不再操作，播放成功，中间有短暂 waiting；跳至 130 秒播放成功后再跳转，无新增 AbortError。说明失败与未完成的播放请求被后续暂停有关，并非每次 seek 都失败。
6. 同时遇到一次 frame/4500 返回 429（trace `9b98c9d5d1e7401f8a04c0d7344336e2`），文案为后台正在处理其他视频。这条抽帧失败应与播放 Promise 拒绝分开分析。

后台标签页 initially stalled、脚本 click 的 NotAllowedError 是自动化干扰，已排除；后续证据来自前台真实控件操作。本次未做持续限速或 Windows Chrome 验证，不能断言已确定最初用户反馈的唯一根因。

## 证据与运行状态

浏览器事件 JSON：服务器备份目录的 browser-evidence.json / all-events.json；本机 `/tmp/exactcut-playback-repro-20261027/` 同时保留截图、复制脚本和观察脚本。项目复制校验清单：`/srv/exactcut-test/playback-repro-20261027.json`。

正式健康始终为 ok，release 为 us-east-20261026-brandjpg-27caf4f、worker_alive=true，StartedAt 前后均为 `2026-10-26T14:11:25.243051422Z`。未重启正式服务、未发布日志补丁、未重跑或替换正式成片。测试实例保留运行，供继续复现；两者同宿主，资源共享，不能承诺性能绝对零影响。

修复候选（本次未实施）：区别被后续用户操作取消的 play 请求和真正的媒体错误；成功 playing 后清除对应播放错误提示。不要仅凭这次复现移除全局锁或修改 Range。
