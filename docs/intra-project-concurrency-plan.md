# 同项目多素材并发 · 实施规范

日期：2026-10-10。分支 `concurrency-refactor`。数据 schema 保持 1。

## 目标与参数（用户确认）

- 典型一个项目 **6 条素材**（request）。
- 全局 `render=4`，**单项目 render 并发上限 `per_project=2`**（**只限 render**；upload/poll 不受它限制，由各自 lane 限量）。
- **批次状态由素材汇总派生**（页面可接受）。

## 1. 模型：并行单位从「项目」下沉到「素材」

```
fetch(batch) ─┬→ render(batch, 素材1) ─→ 确认1 → upload(1) → poll(1)
              ├→ render(batch, 素材2) ─→ 确认2 → upload(2) → poll(2)
              └→ …（同项目 render 最多 2 个并发）
```

- `fetch` 仍**按项目一个作业**（下载整批所需正片；链路带宽单流即饱和，不做项目内并发下载）。
- `render` 改为**按素材一个作业**：`render <batch_id> <request_id>`。
  - 复用现有 `orchestrator.execute_batch(..., request_ids=[rid])`（已支持只渲一条）。
- `upload` / `poll` 本就是按素材，保持不变。

## 2. 数据结构变化（主要工作量）

| 现在 | 改成 | 说明 |
| --- | --- | --- |
| `production/results.json` 整批一个、**无锁** | 每条一份 `production/results/<request_id>.json` | 并发渲染不丢更新；**必须兼容读取旧整批 `results.json`**（存在则合并） |
| `runtime.json` 的 `stage / active_request / executor_running` | 每条一份进度（写进该素材的结果文件） | 避免并发互相覆盖 |
| `runtime.json` 的 `status / message` | **由素材汇总派生** | 批次仍给页面一个总状态 |

- `production/videos/`、`previews/` 已按素材命名，安全。
- `publications.json` 已按素材键 + 锁，**不改**。
- `execution()`（进入置 `executor_running=True`、退出置 False）改为**按素材计数**，批次"是否有执行器"= 任一素材在跑。

## 3. 调度（两处）

1. **围栏键**：`batch_id` → **`(batch_id, request_id)`**。
   - 同素材跨阶段串行；同项目不同素材可并行。
   - **无 `request_id` 的旧任务保持整批串行**（兼容）。
2. **`per_project` 上限**：仅统计该项目的 **render** 在跑数，达到上限则其它 render 跳过（不挡别的 lane、不挡别的项目）。

## 4. 阶段推进器（按素材）

- `fetch(batch)` 完成 → 为该批**每条素材**各创建一个 `render(batch, r)`（用确定性 id，重复完成安全）。
- `render(batch, r)` 完成 → 该素材转"待审片"（不阻塞整批）。
- 用户确认某条 → `upload(batch, r)`。
- `upload` 完成 → `poll(batch, r)`（`not_before` 回队列）。
- 批次状态派生：有失败→`partial`；有在跑/待审/发布→`review`/`publishing`；全部 `fbbm_ready`→`complete`。

## 5. 同剧/同集锁收紧（本次一并做）

1. **缓存命中不再抢独占锁**：`source_cache._ensure_one` 先**不加锁**判断文件就绪（`_usable`），已就绪直接返回；只有确实要下载才获取独占锁。
2. **拿不到锁不占位**：若必须下载但正片被他人占用，返回"等待"（`ResourceDeferred`/退出码 75）回队列，**不占 fetch 名额**，下 tick 重试。
3. **缩基底锁范围**：`renderer` 的基底锁**只包"检查/生成/发布基底"**；生成并发布后立即释放，**Hook 叠加在锁外**。同选段不同 Hook 不再串行。

## 6. 配置

```jsonc
"concurrency": {
  "enabled": true,
  "fetch": 2, "render": 4, "upload": 2, "poll": 4,
  "per_project": 2,        // 新增：单项目 render 并发上限；默认 1 = 现状
  "hook_previews": 1,
  "ai": { "requests_per_minute": 6, "burst": 1 }
}
```

- `per_project` **默认 1**（不配置即等于现状，可一键回退）；生产设 2。
- 只影响 render；非 render 的 lane 不受它限制。
- 沿用现有配置校验习惯（非法值拒绝启动）。

## 7. 兼容与回退

- schema 仍为 1；只改 batch 内部文件，并**兼容读取旧 `results.json`**。
- 已排队/在跑的旧格式任务继续可用。
- 回退：`per_project=1` 或 `enabled=false` → 回到现状，不还原数据。

## 8. 测试（TDD，本机 macOS + Linux 容器都要全绿）

**单元**：围栏键 batch→(batch,request)；`per_project` 只封顶 render（第 3 条不得启动、但 upload 不受限）；批次状态由素材汇总；旧 `results.json` 兼容读取；`per_project` 缺省 1。
**集成**（临时数据根 + 模拟外部）：
- 6 条素材 → **恰好 2 条并发渲染**，其余排队；
- 两个项目各 2 条 → 占满 render=4；
- 同项目 upload/poll 可并行；
- 一条失败 → 批次 `partial`，其余不受影响；
- 崩溃恢复按素材结果重排、不重复；
- 同剧：A 渲染时 B 的"缓存命中"不再被挡（非阻塞）；
- 同选段不同 Hook 不再被基底锁串行。

## 9. 约束

- 单机、单数据根、持久 JSON；不引入新中间件。
- **不改三个前端文件** `source/exactcut/web/{app.js,helper.js,index.html}`（保持不动、不提交）；只 `git add` 自己的文件。
- 不部署、不碰生产、不调用真实上传；测试用临时数据根与模拟客户端。
- 需要 Python 用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。
- 完成后停下，等 grok 复审。
