# Main 合入与独立测试部署（2026-10-13）

用户授权：审核改动归入 main，只发布线上测试实例；正式服务不发布。

- main 从 d22126b 快进到已审核0178862；原未提交副本与快照SHA256一致，先用stash备份再合入，没有丢弃用户修改。
- 83f64c5：段效果测试改为比较效果前后的真实基础帧数，保留画面/字幕/音频校验；Grok复审批准。
- a7ad9af：Linux发现输出 `-frames:v` 会截短复制音轨，改为视频滤镜 `trim=end_frame`；不改变剪辑区间，输出帧数守卫保留。Linux样例修复前91视频帧不变但音频132包变128包；修复后音频哈希一致。本地/Linux定向各8项通过，Grok续审end_turn批准。首次Grok达到工具上限未计为通过。
- 最终Linux：1272 passed，48 skipped，242.70秒。跳过项包括该镜像不具备的浏览器及平台条件测试。
- 最终本地全套：1318 passed、2个环境标记冲突失败；这两项用独立临时data复测2 passed。此前剥离版本全套1320 passed。不得把本次本地全套描述成一次全部通过。
- Linux之前的失败轮次包括不完整测试文件包、运行镜像缺Node、继承worker=0及错误环境标记；均修正后重跑，并发现和修复了上述真实音频问题。

## 部署制品

- 源提交 a7ad9af；仅exactcut/config/templates，未携带data、账号或秘密。
- SHA256：290deab44751025d50db7a559f7a2ac01a2a446982ff9c8ffce6136272e8ef8f。
- 测试标签 test-main-a7ad9af，测试容器exactcut-test；保留6CPU/6GiB、独立data/accounts、test OSS前缀、禁止FBBM/飞书交付。
- 回退目录 `/srv/exactcut-test/code-main-a7ad9af.previous`；不回滚data。
- 完整Linux测试挂载的已提交源码快照加后续测试/音频修复与最终source实现一致；运行镜像依赖锁无变化。
- 可视化选帧仍在独立 feat/visual-frame-picker，不在main；旧剪辑核/马赛克未合入。

## 验收

实际切换后结果追加于下方。正式服务切换前版本us-east-20261012-timing-open-4e70327，StartedAt为2026-10-12T13:53:04.32042892Z。

切换完成并验收：
- 测试health返回test-main-a7ad9af、worker_alive=true；公网/test/healthz为200，无重定向。
- 测试登录、Cookie Path=/test/、页面/app.js/账户/项目列表/原项目详情均200；移除的source-picker.js为404。
- 原测试项目975a9095c6014639b8349962e0538e8a errors=[]、draft；真实Chrome登录态保留，页面加载帧输入、文本识别、段字幕/闪白，无精调入口，无阻断错误。
- 5个原draft文件及密码的SHA256与切换前完全一致；未改时间段、未新建或恢复制作任务。
- test OSS对象前缀exactcut-previews/test、delivery=false、publish/external_writes=0均断言通过。
- 正式health正常、worker_alive=true；版本与StartedAt均与切换前一致，未重启正式容器、未修改Nginx。
- 本轮完成部署与页面/API验收，未发起长视频制作；用户明确要求等待其测试后再讨论正式发布。不得自行发布正式服务。
