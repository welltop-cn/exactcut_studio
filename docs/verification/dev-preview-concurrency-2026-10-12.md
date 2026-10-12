# 本地 dev 字幕并发实际验收

2026-10-12 18:47–18:48（北京时间），Tabbit 可见页面操作，真实本地 EP1 视频，不 mock 制作或预览。

- 保留用户原空项目 5be075ca31c744a78cd930f601018db5，新建后台制作项目 5f0b2b52b5fe40a397c0acc51ab10d74，制作 60 秒 1080×1920 视频。实测 FFmpeg 编码运行。
- 制作中通过页面新建字幕 A，保存并预览成功，页面显示真实视频字幕图，约 5.5 秒。
- 页面创建 B/C/D 三个独立项目，多标签页同时点击保存并预览；HTTP 全部 200，页面图片均显示，耗时 6385/6468/6425 毫秒。
- 18:48:11.661–11.694 日志 active_previews 依次 1/2/3，preview_limit=3；18:48:17.754–17.812 全部释放至 0。18:48:18.861 scheduler.snapshot 仍有 render=1、resident_count=1，预览不计入制作占用。
- 对应 trace：acceb038ad5d42adbd8341d483db447c、679e64f39ebb46a8abfd647b2f8c5c76、2e0cabb95bcb4b378bfd6e3cdffacc24。
- 本地原默认配置预览上限为 1，已通过现有配置加载机制使用私有 development override 设置 render=3、hook_previews=3；重启前无编码进程。启动脚本保留 bin/dev-web 的隔离设置，未开启下载、外部写入、上传或 AI。
- 测试仅覆盖本地真实抽帧与字幕排版、制作并行；不包含真实外部 AI 服务延迟，也不表示已部署生产。

私有配置、启动脚本、日志摘录和截图：source/.dev-state/preview-concurrency-20261012/。保留测试项目供复查，未改用户原项目。
