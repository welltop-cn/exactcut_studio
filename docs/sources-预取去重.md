# sources 预取任务去重

根因：帧格式素材尚未就绪时，`source_prefetch.status()` 已根据活跃任务将状态设为 `queued`，后续帧格式分支却只接受 `waiting`，将其覆盖回 `waiting`。反复请求 sources 因而被当成缺少素材并重复建任务。

修法：帧格式分支接受已有的 `queued` 状态，保留活跃任务的排队状态。`prefetch_sources()` 在建任务前，再按本批次、本实例、同剧且状态为 `queued` / `running` 的预取任务过滤已覆盖集数，只为新增集数建任务。独立模块锁 `PREFETCH_LOCK` 包住任务列表读取、判断、保存和入队，防止并发请求重复创建；不重复获取 HTTP 外层已持有的不可重入 `LOCK`。

边界：`completed`、`failed`、`interrupted`、`cancelled` 不算活跃覆盖；失败任务仍需显式 `retry=True` 才重试，重试也不会重复创建已有活跃任务覆盖的集数。不同批次、实例、剧目的任务互不阻挡。素材就绪、下载中、建索引及失败的判断保持原有行为，不改动剪切、打码、制作任务或前端。
