# 切项目先显示执行进度，预览页保留预下载

## 已确认需求与证据

用户提供 Mac mini 对照实验：无新视频传输时详情 467–562 ms；视频 Range 请求期间详情曾达 43,654 ms；临时禁用预加载切三个项目为 572 / 547 / 1,232 ms。源站另有同 trace 日志：浏览器详情首字节 47,875 ms，后端处理 8 ms。这些证据支持自动视频传输触发共享链路等待，尚不足以确定具体阻塞层。

用户确认保留主动进入「成片预览」后的预下载，切项目进入「执行进度」，切走立即停止旧视频请求。草稿仍默认编辑页。

## 修改

displayBatch 在 renderEditor / renderRuntime 前调用 setTab，已制作项目进入 progress，草稿进入 editor。避免 renderRuntime 继承旧 review 标签并提前触发预加载。冷加载和缓存切换均复用 displayBatch。

保留预览页可见视频自动预加载、不自动播放的行为。保留 openBatch 的旧媒体释放顺序：断开 IntersectionObserver、pause、移除 video/source 的 src、load 取消旧媒体，再发起新项目请求。已有缓存后台刷新不重置用户主动切换的 tab。

## 回归

新增测试在修改前失败，确认两个 renderer 错误继承 review；修改后均见 progress，草稿均见 editor。既有预览主动预下载、切换取消时不释放视频、正常切换 pause/load、乱序响应与缓存保护测试通过：6 passed。node --check 通过。

## 发布验收

- 本地完整回归 848 passed in 129.05s；Linux 发布构建 846 passed。
- 2026-10-10 23:00（上海）通过 linux-ops 发布 `us-east-20261010-progress-switch`，提交 f313c22240d7cb77817274e7f96ea487bc406439，镜像 sha256:5e7f0888328f3bcc8662f92a331557de9e22c18c869fdf6abcb6f5acf46dc1d4。
- 公网 healthz：status=ok、maintenance=false、worker_alive=true、release 匹配。
- 已登录 admin Chrome 刷新后脚本 4f6eeb8014e2171dc7e7，初始 activeTab=progress，预览请求 0。从 review 切项目时立即检查两段旧视频：paused=true、src=null；目标项目 571 ms 返回并保持 progress，新视频 preload=none，切换期间预览请求 0。最终回到用户指定项目，openingBatchId=null。
- 此 Chrome 标签处于后台，媒体请求计数不代替前台 Mac mini 体验；主动进入预览触发可见媒体预下载由现有 IntersectionObserver 回归确认，线上该逻辑未改动。
