# 四素材真实项目：剪切打码后台队列用户试用 dev

日期：2026-10-31。用户明确要求现在启动并保留运行，优先使用此前已经下载的真实项目副本。已使用父 review 通过的代码 `bfeab190d039aaef69278bd8afd76745146efb8e`，没有修改实现代码。

## 入口与归属

- 用户入口：`http://mosaic-queue.localhost:18883/home/#0d6fdddb468348ad92e0cbd8f5de1db5`。
- 账号：`mosaic_trial`，独立 producer 账号；密码仅保存在本工作树 `source/.dev-state/cut-mosaic-user-trial-20261031/credentials.json`（0600），通过私有交接提供用户，不写 Git。
- 项目名称：`剪切打码后台队列测试 · 101682 · 四条真实素材（独立可编辑副本）`。
- 服务 PID：`71555`，核验 cwd 为本工作树 `source`，命令为指定 Python 3.12 `-m exactcut.webserver`；独立 session、stdin 为 DEVNULL，日志写私有文件，父进程退出后仍运行。
- 启动器：`source/.dev-state/cut-mosaic-user-trial-20261031/start.py`。准备器 `prepare.py` 拒绝覆盖既有数据；以后重启只运行 `start.py`，不要再次准备或重置用户数据。
- 独立数据根：本工作树 `source/.dev-state/cut-mosaic-user-trial-20261031/data`。配置 `runtime.json`、仅含 development 标记的 `development.env`、`server.pid`、`code-revision` 和 `server.log` 均在同一私有目录。
- 已按用户要求保留服务运行。后续停止/重启前重新核验进程、cwd 和活动任务，并先与用户/父会话协调，不照抄历史 PID。

## 本地复制与隔离

优先检查了已有的 preview-subtitles 与 release-review 两份数据和准备/导入脚本。最终使用可编辑且无交付关联的本地 release-review 副本：

`/Users/matao/.config/superpowers/worktrees/exact-cut-fbbm-handoff/release-20261030/source/.dev-state/release-review/data/.web-state/batches/0d6fdddb468348ad92e0cbd8f5de1db5`

只复制四条当前原片、四条轻量预览、一个必要结尾图片、当前版本/项目配置及四份身份和源 SHA 匹配的 completed 字幕报告。总计8个视频744300069字节，另结尾图片204724字节。逐一验证文件 SHA、独立 inode、link count=1；采用 `shutil.copyfile`，没有 hardlink。来源清单与校验值在私有 `provenance.json`，包括源配置前后 SHA。

新建 enrollment、registry 与账号归属；不复制源账号库、任务、旧 mosaic session、失败字幕报告或 queue 文件。`deliveries/outbox/mutations` 清空，current versions 保留，candidate/history 清理，执行身份/child_id 等从注册版本中移除；保留原始视频、当前配置和来源标识。源项目的 media-state/draft/runtime/access 四项哈希在复制及验证后均不变。

所有八个视频和必要图片都在新数据根；四份字幕报告保留原 binding 与已验证身份，不修改识别结果，更新的仅为已经校验内容一致的新本地文件 stat 身份。对应99/113/103/110句。没有触发新字幕识别，也没有复制付费凭据。

## 功能与安全边界

- 剪切打码启用；默认队列并发1。手动固定位置打码与剪切可用，结果由用户生成后预览并确认或放弃。
- 扫描/Gemini 与 SAM tracking 均显式关闭；不提供真实 SAM 推理，本地没有为本任务配置模型或凭据。
- 正式制作 worker、素材获取、外部写入、发布全部关闭；OSS 播放/上传关闭。此模式沿用已经验收的本地剪切打码服务配置，mosaic worker 和确认替换不依赖正式制作 worker。
- 原有完成字幕缓存可读取；字幕凭据文件指向本实例不含密钥的 development.env，新识别请求缺少凭据会在入队前拒绝，不能调用付费模型。
- 服务启动环境剔除了继承的 EXACTCUT / Gemini / OpenAI / Aliyun / 飞书 / FBBM / Material Hub 等相关配置，重新显式指定本实例路径和禁用开关。
- 8793/8801/8810/8821/8822 等其他服务未停止、重启或修改；没有下载线上数据或写入线上。

## 实际验证（交接前）

HTTP 验证脚本 `verify.py` 已完成，私有证据 `http-evidence.json`：

- health=ok / development，release=`development-mosaic-queue-user-bfeab19`；正式 worker_alive=null。
- 独立账号 HTTP 登录200，项目200、恰好4素材；四条 `can_edit_video=true`。
- 四条原片和四条轻量预览 Range 均206，每次返回1024字节，Content-Range正确。
- 四条 `/mosaics/open` 均200，元数据帧数分别6187/6399/6020/5878；capabilities region_recognition=false、tracking=false、render_stop=true。
- 四条 latest render 均为空；四条完成字幕缓存可读且句数匹配。

随后使用独立 Playwright/Chromium context，在用户使用的 `mosaic-queue.localhost` 主机完成真实登录和四次编辑器打开：

- 每条原片 source.readyState=4、首帧实际加载；添加片段按钮可用，停止按钮隐藏。
- 4素材卡片均有剪切打码入口。JS错误0、生成/扫描/定位/跟踪/确认等任务POST为0、本机剪切打码草稿0。
- 初次浏览器验证在异步加载时过早采样了一次禁用按钮；修正验证等待条件，等首帧与服务端状态恢复完成后再采样，四条均通过。未修改业务代码或用户参数。
- 私有截图为 `four-materials.png`、`editor-ready.png`；真实内容截图和原视频不随 Git 提交。

最后再次核验：mosaic队列tasks=0（无pending/active），字幕队列为空，制作jobs=0，deliveries=0，outbox=0；当前原片 SHA 与来源清单一致，四条均保持原始可编辑状态。没有替用户预先生成、取消、放弃或确认任何任务。确认路径沿用已经通过独立 review 与1943项完整回归的实现；本轮特意不预跑这四条用户素材的结果确认。

用户可进入「成片预览」依次打开素材的「剪切打码」，自行添加删除段或固定区域后提交，关闭编辑器再打开其他素材继续提交；任务完成后由用户本人预览、确认替换或放弃。
