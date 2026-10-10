# 取消自动视频预加载版本发布

用户要求发布最新代码。最新提交为 `0c6ffbec77a15ecd27492ab888385f271870f76b`：移除预览页 IntersectionObserver 自动加载，视频保留 preload=none，点击播放才下载；保留切项目默认进度页与旧视频释放。发布前已告知本次最新行为区别于上个版本的“进入预览自动预下载”。

2026-10-10 23:28（上海）通过 linux-ops 发布 `us-east-20261010-latest-0c6ffbe`，镜像 sha256:be49029723f811d2c79ef3ddadc5561e616e1db83566ef5df4561fdacf934c20。本地 853 passed in 130.25s，Linux 851 passed / 2 skipped。公网 healthz：release 匹配、status=ok、maintenance=false、worker_alive=true。

线上 admin Chrome 无未保存内容后刷新，新脚本 01a57c8895f8f2f32948；进入 review 后 preparePreviewLoads 不存在，两段视频均 preload=none、paused=true。此为后台标签检查，不代替 Mac mini 前台完整播放验收。

用户同步提供强刷首页首字节等待 33.30 秒截图。只读诊断：检查时服务器 load average=0.83/0.60/0.37，可用内存 29723 MiB；容器 CPU 10.24%、43.29 MiB、20 PIDs。此前 10 分钟已记录的两个首页请求后端各 1 ms，无已记录 http.completed 超 2 秒。不能将当前资源快照当成截图发生时的负载，也未拿到截图该请求 trace_id 做精确关联。代码为 ThreadingHTTPServer，stream_video 不持项目 LOCK。结合既往同 trace 的浏览器 47875 ms / 后端 8 ms，仍应追查媒体/API 共享网络及代理链路，不能声称关闭预加载已经根治播放时的阻塞。
