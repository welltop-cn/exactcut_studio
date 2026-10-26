# 视频水印：剧名与 LOGO

项目表单提供独立的「剧名」「LOGO」开关，默认关闭。新启用的剧名使用封面艺术字；明确保存为 text 的旧配置保留普通文字，不同时叠两种标题。

## 制作流程

1. 制作前按六位剧 ID 查询 KES，下载该剧封面。
2. OpenAI 兼容 `/images/edits` 只保留封面剧名，返回真实透明 PNG。模型可能重建笔画，并非逐像素抠图。
3. 按封面内容、模型、完整提示词与质量设置复用缓存。每剧一把锁，同剧避免重复付费，不同剧可并行；批次固定资产引用以保持重试一致。
4. 图片或引用损坏时，在实时提取启用的前提下自动恢复。另一部剧的资产引用仍拒绝使用。
5. 合成无需单独预览确认。剧名和 LOGO 只作用于剧情，最后一帧/海报结尾不加水印。

1080×1920 画面中，艺术字距左54、距顶42像素，裁掉透明外边后等比放入300×180区域。LOGO 底部水平居中，底边距画面底边110像素。其他分辨率按画面宽度缩放。字幕位置不移动，重叠时字幕覆盖水印；预览与正式输出顺序一致。

## 配置

使用现有集中配置读取机制，`title_extraction` 字段见 `source/config/default.json`。

- `enabled`：允许使用剧名提取功能。
- `allow_live_requests`：允许真实封面查询和图像编辑；默认 false，生产部署显式开启。
- `credentials_file`：可选独立环境文件；生产使用既有 production.env，不把本机路径带入生产。
- `model` / `quality` / `timeout_seconds`：图像请求设置。
- `kes_base_url` / `poster_base_url`：已验证的 KES 和封面来源。
- `kes_connect_ip`：可选的局部连接地址覆盖，只保留给有 DNS 故障的环境；仍使用原域名 Host、TLS SNI 和证书校验。正常服务器不配置本机临时 IP。

凭据字段为 OPENAI_API_KEY、OPENAI_BASE_URL、KES_LOGIN_EMAIL、KES_LOGIN_PASSWORD、KES_LOGIN_ENCRYPTED_PASSWORD。不把密钥提交到仓库或写入报告。封面查询/下载的网络错误最多尝试3次；图像编辑 POST 不自动重发，避免超时后的重复计费。

资产保存在 `<data_root>/title-assets`，需随数据备份。缓存损坏修复后图片可能与旧图有细微差异。禁用实时提取时只可复用完整缓存，不会绕过开关联网。

本地 dev 可使用 `bin/dev-web --title-images --fetch-env <独立开发环境文件>`；只读下载器只下载已有 OSS 对象，不执行远端复制、上传或发布。生产使用既有正式下载流程，不使用 dev_fetch。

`exactcut.title_image` 中 Gemini 生成新标题和纯色抠底的实现是早期实验；正式流程仅使用 `exactcut.title_image.extraction`。`source/experiments/title_extraction` 记录会话图像工具实验，不是生产客户端。

## 验证与发布

相关测试覆盖透明校验、剧绑定、缓存修复、并发、指纹、真实 FFmpeg 层级及结尾无水印。安装包应包含 `assets/brand/*.png` 与说明图 `web/brand-style.png`。发布使用干净提交和 linux-ops，保留 schema 1、现有账号及业务数据。
