# 执行进度展示整理

本机 dev 验收通过，未发布生产。

- 下载进度、素材五阶段条、片段完成数量和无法量化步骤的处理中进度保留；完成后隐藏阶段条。
- 总耗时包含准备/排队/制作/预览，文案明确“其中制作排队”，不使用差值冒充制作时间。
- 素材历史只突出开始、剪辑、字幕、质检、预览、结束和异常，内部步骤收进技术详情；当天时间不重复日期，素材内不重复编号。
- 共用正片状态保留顶部，原项目动态改为折叠操作记录，保留创建、确认、暂停、继续、归档与重试。原始日志不修改。
- AI 摘要取自实际 hook_emphasis.source/cache_reused/highlights；无证据不声称调用 AI。
- 回归：test_execution_events、test_source_prefetch、test_preview_preload、test_queue_view 共31项通过，JS语法与diff检查通过。
- 浏览器使用真实dev素材确认精简历史、耗时、AI结果和操作记录；进入成片预览后2个video均preload=none/readyState=0，观察3.2秒未产生video请求。
- dev启动时曾短暂connection reset，服务就绪后重试正常；重启前队列fetch/render均无运行或等待任务。
