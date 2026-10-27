# 播放修复测试实例验收（2026-10-27）

用户授权仅更新测试实例，不发布正式服务。测试地址：http://8.221.122.75/test/#671c71aa7db44409b1c8cda98220901e 。沿用上轮独立复制项目、视频、账号；未重复制或替换正式素材。

## 首轮候选与真实浏览器结果

部署 607bde9，release=test-playback-607bde9。白名单 Git archive 包 SHA256：056245ada09065cc8229279fe6fff372fbf491f63cb8bd6be7791a85fecb261c。完整本地回归 1668 项通过，退出码 0；日志 /tmp/exactcut-playback-full-suite.log（pytest 双 quiet 未打印汇总，按进度行统计全部通过）。仅有既有 fork 多线程弃用告警。

公网 Chrome/macOS，前台原生按钮和播放头操作：

- 原问题视频（228.1秒）正常播放成功，readyState=4、media_error=0，时间推进。
- 跳至第6200帧→点播放→立即跳至第3000帧，实际捕获 AbortError，readyState=1、seeking=true、media_error=0；界面没有误报无法播放，再次点击能正常播放。操作间隔约12ms，证明竞态处理，不能证明历史反馈人的操作速度。
- 服务端实际记录 media_play_rejection，trace=eea3f1bd69ee4049a266a503ccc82333，项目、素材、会话、错误名称、paused/seeking等字段齐全；恢复 media_playing trace=ffabdb13789e4b6fa2a8aa7ff222a762。
- 仅在当前浏览器页面临时将视频 src 指向无效 data:video/mp4 样本，原生媒体 error=4，点播放得到 NotSupportedError，界面明确显示“暂时无法播放，请重试”。恢复原 src 后成功 playing，播放错误清除，原 Gemini 禁用提示保留。未改服务器视频。失败 trace=29735dc0c3e94c1eb349ab5a618dd7f3；恢复 trace=bd132a5241d4471a9fca00d2872a51ef。随后持续播放超过80秒。
- 发现附带问题：播放中关闭并重开后，video.paused=true、readyState=4，但按钮残留“暂停”。暂停后紧接 removeAttribute(src)/load 的生命周期不能依赖异步 pause 事件来复位文字。后续候选在两个编辑器 pausePlayback 中同步设为“播放”；4个新增回归（两个编辑器×暂停/重开，无 pause 事件）在修改前全部失败。

本轮页面临时观察器只记录媒体事件和原生 play Promise；返回原 Promise，不改变播放决策。无效媒体测试为明确故障注入，不是历史事故根因证据。

## 隔离与边界

runtime.json SHA256 保持85090258441686777fa4c518e2d1357a9a672694edc302a218d518e062d48ec6。worker、AI、material fetch、publish、external writes关闭，scan/delivery/OSS预览上传关闭；6CPU/6GiB/256PID不变。使用本轮 reproduction 启动器派生脚本，未运行历史 recreate.sh。测试为production环境标记，与正式服务数据目录、端口、容器独立；共享宿主机资源。

完整测试在本机运行，服务器只做Web/视频读流与少量抽帧验收。未重新编码整片、触发AI识别、替换成片或发布素材。旧剪切独立界面及生成结果切换由自动化回归覆盖，公网页面本轮使用统一“剪切打码”编辑器。未覆盖Windows Chrome及持续限速/断网，不能推及所有播放失败已消除。

原始证据：本地 /tmp/exactcut-playback-acceptance-607bde9/；测试服务器部署档案 /srv/exactcut-test/deployments/playback-607bde9/。保留原测试代码 code-playback-607bde9.previous，不回滚或删除数据。

## 按钮补丁审查

Pi deepseek/deepseek-flash、thinking=high、只读工具复审通过，无阻塞项；原文见 [按钮补丁审查](playback-button-pi-review-2026-10-27.md)。审查为静态源码检查，未由审查者执行测试；其中浏览器任务机制解释和其他理论低风险项不是本轮实测证据。已知低风险建议（旧剪切 close 护栏、其他拒绝路径按钮文案）未扩大到本次修复。部署完整性以 Git archive 包SHA256和实际三个JS文件哈希验证，不依赖旧MANIFEST清单。

按钮补丁后完整本地回归1672项全部通过，无跳过或失败，退出码0（/tmp/exactcut-playback-full-suite-final.log）；其中播放回归28项。JS语法检查与git diff --check通过。
