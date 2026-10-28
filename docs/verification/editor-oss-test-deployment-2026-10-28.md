# OSS 正式原片播放：线上测试实例验收

2026-10-28，仅部署独立测试实例，未发布正式服务。

## 部署

- 入口：http://8.221.122.75/test/ （HTTP；不能换成 HTTPS）。
- release：`test-editor-oss-0a3825b`，应用提交 `0a3825b`。
- 从干净 detached worktree `/tmp/exactcut-editor-oss-0a3825b` 验证；归档仅包含该提交的 `source/exactcut`、`source/config`、`source/templates`。主工作区无关未提交修改未包含。
- 包 SHA256：`e3752df9079302d75a51a4589e03f46b8ca793c215bd6ca3aaa883ca55048c7b`。
- 干净提交完整回归：**1723 passed, 1 skipped, 10 warnings，422.47 秒**。Python 3.12，日志 `/tmp/exactcut-oss-clean-release-tests.log`。跳过不适用于旧剪切编辑器的对比用例；警告来自既有 fork 测试。
- 复用 `exactcut-test:mosaic-75efc57` 运行镜像、既有 CPU/内存限额。
- 启动脚本 `/srv/exactcut-test/deployments/editor-oss-0a3825b/recreate.sh`，沿用 playback-62c42b3 隔离参数。worker/AI/material-fetch/publish/external-writes 仍为 0，scan/delivery 关闭。
- 只启用 `cutting.oss_playback_enabled=true` 和 `preview.oss_enabled=true`，`preview.namespace=test`；默认发行配置仍关闭编辑器 OSS 播放。
- 回滚代码 `/srv/exactcut-test/code-editor-oss-0a3825b.previous`；原 runtime 在 deployments 同目录 `runtime.previous.json`。回滚只切代码与配置并按对应安全脚本重建测试容器，不回滚数据。

## 实际公网 Chrome 验证

1. 测试实例原先停止，入口曾返回 502；新容器启动后公网 health 正常，Chrome 进入已登录工作台并打开项目。
2. 使用既有独立副本项目 `671c71aa7db44409b1c8cda98220901e` 的 `R359de48a`，149,968,786 字节、228.1 秒、30 fps。通过既有 original 上传/回执函数补充 **test 前缀**回执；不是 source-url 请求触发上传。签名 GET Range 返回 **206 / 1024 字节**，与本地正式 master 前 1024 字节一致。
3. 剪切打码主播放器真实来自阿里云 OSS，未使用轻量预览。暂停定位到帧 2700，显示 `01:30:00`，媒体位置约 90.001666 秒；恢复播放后 8 秒内从 90.091789 到 98.092177，readyState=4、无媒体错误。
4. 在自己的验收标签页主动派发媒体 error 事件：第一次仍为 OSS 并恢复播放；第二次切到会话本地 `/source`，位置从 116.063765 继续到 119.110060，未暂停。此项是故障注入，不代表真实网络故障复现。
5. 项目 `4142628fb23746149326953e8b8dd49c` 原先已放弃会话对应的旧结果返回 409，符合 reset 校验。通过当前会话创建新的测试渲染 `5a5268890546c2955e024f10ad02efc5`，删除帧范围 `[60,4500)`，得到 60 帧/2 秒/1,509,734 字节结果。未替换 master、未提交交付、未改素材草稿。
6. 关闭重开编辑器，查看新结果：“处理后”来自该任务服务器 `/render/{jid}/video`，duration=2、readyState=4，实际播放推进；下载地址与结果地址一致。“处理前”来自 OSS，duration=150，并与主播放器 **完整 src 相同**。切回“处理后”仍为 2 秒任务结果。
7. 验收自建标签页已关闭；测试容器继续运行供用户测试。未记录签名 URL、账号密码或 OSS 凭据。

本轮没有真实多人并发/用户故障网络测试，也未等待签名自然过期；续签、手动切源、迟到响应等边界由本地回归覆盖。不能据此宣称消除所有卡顿。准确帧仍走服务器。

## 正式实例核对

测试部署前后正式 health 均正常，release 均为 `us-east-20261027-playback-a5f7633`；worker 存活，StartedAt 均为 `2026-10-28T02:55:16.463075318Z`。没有重启或更新正式容器、修改正式配置或 Nginx。
