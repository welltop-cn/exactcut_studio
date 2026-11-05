# 18883 本地并发候选升级记录（2026-11-03）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

父最终只读review通过并明确授权后，仅更新本地用户18883。当前入口仍为：

**http://mosaic-queue.localhost:18883/home/#0d6fdddb468348ad92e0cbd8f5de1db5**

- 当前PID：**15988**。
- 加载提交：`e2cbcc08fd28c1adef4daca0bca503e220481e83`；对应应用源码`96c1aa555e35c394178723afd7d65e22c0142597`。
- 代码树：`cut-mosaic-resource-scheduling/source`；原数据根、账号、素材与启动器路径保留。
- 未发布生产、未合并main、未取消用户任务、未重新运行模型容量测试。

机器可读证据：[JSON](frontend-background-user-upgrade-2026-11-03.json)。实现及新源码2057通过/1跳过/10既有警告的验证见[固定候选验收](frontend-background-resource-scheduling-2026-11-02.md)。父已独立完成原36项、补丁14项及生命周期/outbox/cancel专项review；最终另做独立health核验。

## 空闲与正常切换

初始只读核实实际监听PID59032、命令、cwd和启动器，确认原树源文件与原已加载版本一致。逐项检查final、scan、locate、track、preview、Hook磁盘预留、普通jobs、字幕、session内任务和子进程；没有active、queued或不可读状态。不存在会在维护期间自动启动而丢失的pending。

在原mutation屏障内取得计算/识别/帧/准备及已存在worker/交互资源锁，设置本次维护标记后再次复查空闲，才SIGTERM旧服务。没有强制kill，也没有取消用户任务。原启动器、runtime、PID和revision标记逐字节备份，账号库另有私有一致性备份；普通回退没有恢复旧DB。

首次新服务已通过健康、配置、dispatcher和持久状态核验，但脚本静态JS请求漏带认证而401。按预案正常停止该实例，逐字节恢复旧启动器/runtime，以旧代码重新启动PID14277；未恢复或覆盖用户数据库。修正核验认证后重新执行空闲/维护检查，再正常切换至当前PID15988。失败记录及两次备份均保留，不将第一次失败写成一次成功流水。

## 保留状态与配置

|核验|结果|
|---|---|
|重启前受保护文件|336份，既有JSON、媒体、账号库、口令/凭据文件均在私有哈希清单中|
|重启后、浏览器登录前|所有既有受保护文件SHA完全一致；新增空交互队列不含pending/active|
|实际登录后|除认证会话库的预期变化外，所有既有受保护文件SHA仍一致；users表逻辑哈希不变|
|限流桶|原ai-rate.json字节哈希不变；没有清空、重建或换域|
|四素材与旧结果|认证GET四素材可读，原片Range206和completed结果Range206通过；旧cancelled/reset不匹配仍正确409|
|旧冻结/决定状态|所有任务/媒体JSON字节一致，没有确认、放弃、reset或修改草稿|
|交互dispatcher|worker0下由当前PID持有真实dispatcher owner lease，非仅看HTTP健康|
|静态前端|app.js、mosaic.js、cutting.js、editor-busy.js的响应字节与已审代码树一致|
|独立浏览器|真实登录、四入口、JS异常0；不接触用户标签页/localStorage，未打开会自动识别的编辑器|
|自动任务|模型/预览/生成POST为0；仅允许login、同项目workspace和client-events|

沿原集中runtime仅修改本次并发覆盖：普通fetch2/render3/upload2/poll4，删除旧总1覆盖并推导总11；final1；字幕2+30；交互preview/scan/preprocess各2；媒体共享5+final1、frame2、transfer2；FFmpeg4、交互encoder2、SAM4；其他OpenAI6RPM/burst1。旧per-user和Hook数量覆盖删除。

`EXACTCUT_WORKER_ENABLED=0`、外写/发布/素材获取禁用、原真实SAM/Gemini配置及**MPS设备**均保留；cutting处理配置没有改写。MPS是设备环境差异。本地实际有效配置等于同版本候选生产业务基线，不声称已读取线上生产runtime；媒体5+1也不等于Linux生产最大承载核定。

## 留存与后续

私有脚本、逐字节备份、前后哈希及完整核验放在原用户trial目录下`resource-upgrade-20261103-005550`（首次正常回退）及后续成功目录；成功路径在`resource-upgrade-latest`标记和公开JSON中。没有删除旧树或用户结果。

服务保留供用户继续使用，不为进一步验证自动提交用户素材任务。当前等待父独立health核验。范围外已确认问题：**无**。
