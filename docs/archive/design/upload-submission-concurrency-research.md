# 成片多素材上传：并发提交与后台队列调研

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 范围与结论

调查代码基线：`3f2ef04`。仅本地源码、既有排障记录及隔离 fixture；未访问生产、未上传 OSS、未写飞书/FBBM，未修改业务实现。截图诊断号 `06d677b01ba443e099117bdb76bed17a` 未在本地 docs/source 找到，尚不能确认该线上请求的具体时序或部署版本。

后台已有持久化上传队列，无须从零新建。当前代码允许不同素材提交，但纳管项目上传入口使用整个项目的 revision 校验：连续点击时，两条请求携带相同 revision，第一条入队增加 revision，第二条被拒绝，错误文本与截图一致。后台交付状态合并也增加项目 revision，所以即使等第一条响应后再点，仍可能遇到 worker 推进版本的竞争。

## 直接证据

- `source/exactcut/web/app.js:20`：`api()` 自动附加当前项目 revision，并维护 submission_token；没有项目级提交队列。
- `source/exactcut/web/app.js:32`：`operation()` 只以当前按钮去重；不同素材按钮可以同时发请求。
- `source/exactcut/web/app.js:453`：上传按钮按该素材的交付状态禁用；已有文案“已进入队列…可以继续提交其他素材”。
- `source/exactcut/web/app.js:503`：点击调用 `/publish`，携带 request_id、version_id、sha256；响应直接赋值 current，没有像轮询那样校验 revision 单调性。若将来允许并发接受，需要一起避免响应乱序覆盖新状态。
- `source/exactcut/webserver.py:1559`：纳管 publish 调用 queue_delivery，成功返回 202。
- `source/exactcut/material_versions.py:227`：`_mutation` 先检查幂等回执，再严格比较项目 revision；不一致直接抛“项目已在其他页面更新，请刷新后重试”，尚未执行写入交付的 operation。
- `source/exactcut/material_versions.py:434`：queue_delivery 检查成片 SHA、当前 version、暂停/归档/删除、同素材阻塞交付和文件名冲突，原子写 delivery 与 outbox。不同素材本身不互相阻塞。
- `source/exactcut/material_workers.py:83`：`_merge_delivery` 在交付状态变化时增加父项目 revision，草稿可以完全不变。
- `source/exactcut/material_workers.py:107`：recover_outbox 根据持久化意图恢复子任务，生成 queued upload job；已有恢复与防重复机制。
- `source/exactcut/webserver.py:869`：调度器读取持久化 queued jobs，并按资源池容量准入。
- `source/exactcut/concurrency.py:25`、`source/config/production.json:14`：存在独立 upload/poll 池；仓库 production 配置 upload=2、poll=4，不代表线上有效配置已核验。upload 改为 1 可以限制本实例上传池串行，无须关闭整个 concurrency；该池也承载映射到 upload lane 的其他工作，配置影响范围不只当前项目。
- `source/exactcut/publish.py:391`：上传链路是 OSS → 飞书创建/复用待处理记录。
- `source/exactcut/publish.py:440`：后续只读查询飞书自动化回写结果，不在这里直接发起 FBBM ingest。限制本服务 upload=1 不保证外部自动化/FBBM 全链路串行。

## 隔离复现

临时探针：`/var/folders/mb/7rs6fvr50gvc8yc93d73bvn00000gp/T/exactcut-upload-research.y2GSEP1sWd/test_upload_research.py`。

从现有 test_workflow.workspace/imported/complete_asset fixture 建立两份成片，纳管后执行真实 queue_delivery、recover_outbox、_merge_delivery；交付开关与目标指纹使用隔离替身，没有运行任何外部交付进程。

```sh
PYTHONPATH=source:source/tests ~/miniconda3/envs/py3.12/bin/python3.12 -m pytest -q -s /var/folders/mb/7rs6fvr50gvc8yc93d73bvn00000gp/T/exactcut-upload-research.y2GSEP1sWd/test_upload_research.py
```

结果：`1 passed in 0.37s`。这里“通过”表示探针成功验证现有缺陷，非修复完成。

1. 两个素材使用同 revision，第一条成功，第二条精确抛截图错误。
2. 第二条改用第一条返回的 revision，成功持久化两条交付；recover_outbox 产生两条 queued upload job。
3. 仅合并上传状态，项目 revision 增加 1，draft 不变。

这是等价请求交错的确定性复现，未做真实浏览器双击或线上日志回放。另曾运行更大范围的既有测试，但未取得完整汇总，不把它记为全套通过。

## 建议方案（待实施）

### 用户体验

允许连续点击 A、B、C 的确认上传，每张卡片立即显示提交中；服务端完成持久化后才显示已入队。某条失败单独展示，其他素材继续。离开页面后只有已被服务端接受的任务才能保证继续，不能把浏览器内等待发送的请求说成后台任务。

### 提交入口

优先将上传确认的并发前置条件从整个项目 revision 收窄到用户确认的素材身份（request_id + version_id + SHA），并在现有锁内重新检查当前素材、项目控制状态、已有交付及文件名约束。保留幂等键与 outbox；草稿保存、编辑等继续保留严格项目级冲突检查。不能全局删除 `_mutation` 的 revision 检查。

实施时应核对切割/打码替换 gate，以及“草稿已修改但当前旧成片仍在”的确认规则，保证不会上传用户没有确认的新文件。仅因其他素材或后台状态变化，不应拒绝上传当前已确认的素材。

前端可按项目串行发送短暂的“入队请求”，改善响应顺序，但不能依赖它解决多页面、多用户或 worker 造成的冲突。网络结果不明时，必须沿用原请求身份查询/重放，不能换幂等键盲目新建任务。

### 执行阶段

复用已有持久化队列。若产品目标就是后台串行，可设置本实例 upload=1，poll 保持独立；A 上传 OSS 并提交飞书后即可释放上传槽让 B 开始，不必等 A 在 FBBM 完全就绪。若要求“直到 FBBM 就绪才允许下一条”，则是另一个跨服务串行策略，需要同时检查外部自动化，不建议误用上传槽来实现。

队列状态需区分提交中、已入队、上传中、平台处理中、已完成、失败/待核实。现有调度是按 owner 公平分配，不能未经改动承诺全局严格 FIFO 或准确队列名次。

### 验收重点

- 同项目三条素材连续提交全部持久化；后台状态推进不能造成无关素材提交失败。
- 同素材重复点击、多窗口同时提交只产生一条有效交付；成片替换、删除、暂停、归档仍严格拦截。
- upload=1 时最多一个上传执行器，其他素材可继续入队；poll 不占上传槽。
- 服务重启恢复、网络超时重放、回执不明待核实都不造成重复外部记录。
- 前端切项目、响应乱序、单项失败不污染另一素材状态。

## 当前决策建议

采纳“前台连续提交 + 后台持久化排队”的体验。主要工作是修复入队入口的并发校验和前端提交状态；已有队列可直接复用。是否把现有 upload=2 降到 1 是资源策略选择，并不是解决截图冲突的必要条件。
