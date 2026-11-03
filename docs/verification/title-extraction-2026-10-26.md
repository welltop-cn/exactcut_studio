# Automatic cover title integration — 2026-10-26

## Final behavior

- The existing 视频水印 / 剧名 checkbox remains off by default. Newly compiled
  drafts with it enabled use the cover-derived title automatically, without an
  image preview or approval step. Confirmed legacy manifests remain readable.
- Resolve a cover from KES by drama ID; verify the returned ID; read `poster` from
  the single matching series record. The login/list contract follows novel-auto.
- POST to the configured OpenAI-compatible `/images/edits` endpoint using the
  observed `gpt-image-2` model, source image, preservation prompt,
  `background=transparent`, and `output_format=png`. Validate actual PNG alpha;
  never silently fall back to plain white text.
- At 1080×1920, crop outer transparent padding, preserve aspect ratio, and fit
  inside 300×180 at (54, 42). Other resolutions scale with width. The title goes
  on the plot only. Logo remains centered at the bottom; ending cards have neither.
- Store immutable images under `<data root>/title-assets/`, bound to drama ID and
  image hash. New live preparations compare cover hash, model and prompt version
  before reusing an image. Per-production pins keep retries stable. Images must be
  retained/backed up with the data root; they are not disposable render caches.

## Configuration

The central `title_extraction` section contains feature enablement, model,
quality, timeout, KES/OSS origins, `allow_live_requests`, and `credentials_file`.
Live requests default off. The dedicated credential file uses existing names:
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `KES_LOGIN_EMAIL`, `KES_LOGIN_PASSWORD`,
`KES_LOGIN_ENCRYPTED_PASSWORD`. Credentials are read locally, not injected into
the process environment. Existing global upload/publish/AI flags remain unchanged.

Local dev was configured through ignored
`source/.dev-state/title-images/runtime.json` and a mode-0600 credential file.
Start it with `EXACTCUT_PYTHON=~/miniconda3/envs/py3.12/bin/python3.12
./bin/dev-web --title-images` from `source`. This enables only the scoped title
query/editor capability. Do not commit or copy these local credentials to releases.

## Evidence

- Actual KES query for 101607 returned `The Alphas I Tamed Want Me in Bed` and a
  valid 258×380 PNG poster.
- Actual compatible service `/models` advertised `gpt-image-2`. Image edit
  returned HTTP 200 with `data[0].b64_json`; one response reported
  `model=gpt-image-2-codex`. This is provider metadata, not proof of the model used
  by the separate Codex built-in image tool.
- The integrated Python client independently completed login → series lookup →
  cover download → image edit → validated transparent asset storage.
- A synthetic 1080×1920 video was passed through `execute_batch`: 2 seconds of
  plot plus a 1-second last-frame ending. QC passed, title and logo were visible
  on the plot, and absent from the ending. A second execution reused the output.
- Private acceptance files: `output/title-pipeline-smoke/acceptance.json`,
  `plot-frame.png`, `ending-frame.png`, and `render/videos/title-smoke.mp4`.
  This is a synthetic test video, not a remake of a user's existing project.
- 120 focused tests passed across title extraction, brand overlays, title image,
  composition, static endings, intake, material versions and hook preview suites.
- JavaScript syntax, shell syntax and `git diff --check` passed.
- Local dev restarted on port 8793 after checking there were no queued/running
  jobs. Health returned OK with a live worker. Production was not deployed.

The editor may reconstruct letterforms and decorations. Alpha validation detects
opaque/empty images, not spelling or visual fidelity. No manual approval gate is
enforced, per the user's requested automatic workflow.

Official contract reference:
https://developers.openai.com/api/reference/resources/images/methods/edit

## Pi review 后修复

- 用户最终要求字幕位置不动，重叠时字幕位于剧名上层。预览、普通合成及片段特效路径统一为先水印后字幕；真实 FFmpeg 测试验证字幕期间上层像素、字幕结束后水印保留。带水印的输出和预览身份新增 layer version 2。
- 缺失/截断图片、损坏的元数据及 latest/pin 指针视为缓存未命中。实时提取启用时自动恢复；禁用实时提取时仍不绕过配置。新图成功后才原子更新引用。内容地址相同也能修复损坏文件，临时文件使用唯一名称。
- 生成锁按剧 ID 分开；同剧仍串行检查和生成，防止重复付费。双线程测试验证不同剧同时进入封面获取，同剧只调用一次图像编辑。
- package-data 包含 assets/brand/*.png；在隔离临时目录实际构建 wheel，确认两张 logo、extraction.py 和 dev_fetch.py 均存在。
- 23 项剧名提取及水印测试通过。此次没有调用真实图像 API、没有重新制作用户视频。

## 正式发布前完整回归

首轮完整回归发现旧测试把新增默认 brand_overlay 算入“历史”指纹，已修正历史fixture；两项测试依赖工作目录环境，改用独立临时数据根；用量页的JS片段测试受文件尾追加事件绑定影响，已将绑定移至对应初始化区。额外核查发现新增 title_image/title_extraction 服务配置会改变未启用水印的历史输出指纹，现从媒体指纹排除这两段；实际图片身份由已解析的 brand 资产哈希表示。新增回归断言旧配置与加入新服务配置后输出指纹相同。上述相关36项测试通过，完整回归重新执行后再发布。
