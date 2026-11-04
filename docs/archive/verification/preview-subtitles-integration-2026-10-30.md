# 预览中文字幕集成验收（2026-10-30）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

状态：本地 dev 已验证，feat/preview-subtitles 未提交、未合并、未部署生产。代码基于 1538619。

## 实现与 review

在原有素材卡片下增加生成开关、状态、时间轴入口和“中文字幕仅供预览，不影响下载或上传的成片”提示。只使用浏览器 TextTrack / VTTCue（line=2），没有第二份 HTML 中文字幕叠层。识别结果不进入 render、mux、download 或 publish。

后台使用持久 FIFO，1 个活动任务、默认 20 个等待任务；相同版本幂等，关闭开关只隐藏字幕，不取消后台任务。排队任务重新检查账号、项目权限、当前成片与识别配置；恢复时不自动重发已开始的收费请求。

审查后修复：dispatcher.stop 超时不能提前释放仍在运行任务的调度锁；恢复失败不能清空 active；入队成功但 report 未写入的崩溃窗口必须由真实 worker 恢复；缺失/损坏账号库不能凭空授予 admin；SDK schema 转换使用 deepcopy，缓存指纹不受原地修改影响。HTTP 契约已同步 queued 状态。

## 验证

指定 Python 3.12 运行 test_preview_subtitles、test_preview_subtitle_queue、test_preview_subtitle_recovery、test_preview_subtitle_auth、test_preview_subtitles_ui、test_first_load：46 项通过。git diff --check 通过。未将全仓回归当作已完成；发布前仍需按仓库规则完成发布检查。

真实隔离本地页面 8821，从现有卡片点击开关；指定 60.1 秒原片均分两段，各 30.05 秒。真实 Gemini 调用 38.375 秒，两段完成，21 条字幕。任务 ID 98053b422f9877f50ed2fa61a39f60ddece0e29427edbff34ce5390bfac1b26e。队列最终 sequence=1、active=null、pending=[]。关闭/重新开启立即复用缓存，没有增加队列任务。

Chrome 实测：只有 1 条原生字幕轨、21 个 cues；时间轴 21 行、点击句子跳转后关闭；1.125 秒原生字幕“你为什么选择乔纳？”与画面对白一致；播放从 14.500462 秒推进至 27.115529 秒，当前字幕“乔纳救了你？”；关闭为 disabled，重新开启为 showing。8 秒间隙无字幕。后台标签完成不抢弹窗，手动打开时间轴正常。当前轮原生全屏请求因后台标签缺少用户激活被浏览器拒绝，未冒称完成全屏视觉验收；单轨与生命周期有自动化覆盖。

正式下载接口经登录浏览器读取返回 200、37789162 字节，SHA-256 f2d7b2ce954b87313d661087bd879e43f45f195ce62a0e7db1e885a7ea84d683，与原片版本记录一致，确认字幕未写入下载视频。

队列多任务 FIFO、失败推进、重复入队、容量、停止锁、崩溃恢复通过隔离测试验证；本轮真实收费验收仅调用指定一条素材，不声称已经进行多素材同时收费测试。自动翻译和时间边界仍需人工检查，不等同平台违规审核。

## 本地入口与证据

http://subtitle-preview.localhost:8821/home/#91f0aaa0aae54aceb28093ea8206b7d2

服务历史 PID 32645，启动器 source/.dev-state/preview-subtitles/start.py；操作前重新核验。独立账号、registry 和数据，外部发布/写入及正式 worker 关闭。保留服务供用户查看，未动 8793/8800/8801/8810/8820。

私有证据：source/.dev-state/preview-subtitles/review-native-subtitle.png，data/.web-state/queue.json 及该项目 preview-subtitles 目录的 report.json / response-*.json。不得将账号或模型密钥纳入版本管理。
