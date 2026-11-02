# 父实现 review 与用户本地试用更新（2026-11-02）

父会话于2026-11-02批准本轮实现review，可安排用户本地18883升级，不发布生产。核查范围包括504359c资源、permit、缓存、取消FD、HTTP/UI和独立MPS差异；独立专项 **45 passed / 23.59秒**，覆盖 editor_resources、interactive_admission、interactive_permits、inference_cache_concurrency、cache_parent_death、processing_group_cancel、processing_stage_leases、preview_lifecycle_ui、preview_ranges、mps_transfers。

父会话还核对了最后完整 **1995 passed / 1 skipped** 日志及真实A/B、CPU冒烟证据。该review不将Mac CPU/MPS测试视为Linux服务器容量认证。

本地交付规则：先只读核查所有最终、识别、定位、track、preview和媒体子进程；有活跃任务就不取消、不重启，报告并等自然完成。空闲时使用维护屏障一次正常SIGTERM重启；停止旧服务后才改变启动器代码路径和集中配置，避免热混前后端。保留原URL、数据根、账号、草稿、session、任务结果、模型配置/凭据和持久AI桶，不重跑prepare或重建项目。保留SAM/Gemini启用、最终1、交互各2/每账号1；AI沿既有集中concurrency路径6RPM/burst2，不改其他lane行为、不清桶。

更新前后以持久JSON/媒体与账号数据库摘要核对。真实登录会建立新的认证session，因此先在登录前核对数据库字节哈希，再核对用户/密码逻辑行摘要；用户素材session不改。验证GET能力/四素材/旧任务与视频Range，浏览器不触发识别、预览或生成POST。回退使用旧启动器与配置字节，不覆盖用户数据。

实际revision、PID、哈希及检查结果将在私有交付证据中记录，并另提供公开脱敏摘要；本文件只是review批准及交付约束记录，不提前宣称升级完成。
