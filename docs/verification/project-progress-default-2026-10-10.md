# 切项目先显示执行进度，预览页保留预下载

## 已确认需求与证据

用户提供 Mac mini 对照实验：无新视频传输时详情 467–562 ms；视频 Range 请求期间详情曾达 43,654 ms；临时禁用预加载切三个项目为 572 / 547 / 1,232 ms。源站另有同 trace 日志：浏览器详情首字节 47,875 ms，后端处理 8 ms。这些证据支持自动视频传输触发共享链路等待，尚不足以确定具体阻塞层。

用户确认保留主动进入「成片预览」后的预下载，切项目进入「执行进度」，切走立即停止旧视频请求。草稿仍默认编辑页。

## 修改

displayBatch 在 renderEditor / renderRuntime 前调用 setTab，已制作项目进入 progress，草稿进入 editor。避免 renderRuntime 继承旧 review 标签并提前触发预加载。冷加载和缓存切换均复用 displayBatch。

保留预览页可见视频自动预加载、不自动播放的行为。保留 openBatch 的旧媒体释放顺序：断开 IntersectionObserver、pause、移除 video/source 的 src、load 取消旧媒体，再发起新项目请求。已有缓存后台刷新不重置用户主动切换的 tab。

## 回归

新增测试在修改前失败，确认两个 renderer 错误继承 review；修改后均见 progress，草稿均见 editor。既有预览主动预下载、切换取消时不释放视频、正常切换 pause/load、乱序响应与缓存保护测试通过：6 passed。node --check 通过。
