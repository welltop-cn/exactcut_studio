# 父实现 review 与用户本地试用更新（2026-11-02）

父会话于2026-11-02批准本轮实现review，可安排用户本地18883升级，不发布生产。核查范围包括504359c资源、permit、缓存、取消FD、HTTP/UI和独立MPS差异；独立专项 **45 passed / 23.59秒**，覆盖 editor_resources、interactive_admission、interactive_permits、inference_cache_concurrency、cache_parent_death、processing_group_cancel、processing_stage_leases、preview_lifecycle_ui、preview_ranges、mps_transfers。

父会话还核对了最后完整 **1995 passed / 1 skipped** 日志及真实A/B、CPU冒烟证据。该review不将Mac CPU/MPS测试视为Linux服务器容量认证。

本地交付规则：先只读核查所有最终、识别、定位、track、preview和媒体子进程；有活跃任务就不取消、不重启，报告并等自然完成。空闲时使用维护屏障一次正常SIGTERM重启；停止旧服务后才改变启动器代码路径和集中配置，避免热混前后端。保留原URL、数据根、账号、草稿、session、任务结果、模型配置/凭据和持久AI桶，不重跑prepare或重建项目。保留SAM/Gemini启用、最终1、交互各2/每账号1；AI沿既有集中concurrency路径6RPM/burst2，不改其他lane行为、不清桶。

更新前后以持久JSON/媒体与账号数据库摘要核对。真实登录会建立新的认证session，因此先在登录前核对数据库字节哈希，再核对用户/密码逻辑行摘要；用户素材session不改。验证GET能力/四素材/旧任务与视频Range，浏览器不触发识别、预览或生成POST。回退使用旧启动器与配置字节，不覆盖用户数据。

## 本地18883实际交付

已在空闲审计通过后仅进行一次正常SIGTERM重启：70585 → **59032**。旧运行版本 `1a8d7c0`；新运行版本 **c93dc49047f4c636a445ccd289f472b18c5fbdff**。先停旧服务，再将私有start.py指向隔离已提交树 `cut-mosaic-interactive-resources/source`；原数据ROOT、URL、账号、项目和模型启用状态不变。原工作树未快进、仍clean；可通过私有备份的启动器和配置回退，不覆盖数据。

公开脱敏证据：[cut-mosaic-interactive-user-upgrade-2026-11-02.json](cut-mosaic-interactive-user-upgrade-2026-11-02.json)。334个持久文件在重启后、真实登录前字节SHA完全一致。登录后仅认证session数据库发生预期变化，用户/密码逻辑行摘要不变，其余333个保护文件仍一致，包括媒体、素材session、草稿、任务结果、凭据和原AI桶。初次验证遇到历史cancelled任务409，已确认属于旧reset轮次，错误与旧版保护一致；没有重写该任务。当前completed任务和结果视频可读。

真实独立浏览器登录通过；用户项目4条入口、4个source Range206、已有完成结果Range206均通过。全部4素材tracking/region_recognition/scan.enabled为true。健康release匹配c93dc49、maintenance=false。真实页面仅执行登录、同项目workspace导航和诊断POST，未打开会自动识别的编辑器，没有识别/预览/生成POST，未操作用户原tab或localStorage。集中配置最终1，交互各2/每用户1、encoder2；其他lane显式保留1、max_active_children1，AI6RPM/burst2复用原桶。

可刷新试用：`http://mosaic-queue.localhost:18883/home/#0d6fdddb468348ad92e0cbd8f5de1db5`。这是本地dev交付，不是生产发布或Linux CPU容量认证。当前Mac原有device=mps保持未改；服务器CPU主线与Mac兼容修复仍为独立提交。后续仅记录本交付的文档提交不改变已运行的代码文件，health运行release仍为c93dc49。
