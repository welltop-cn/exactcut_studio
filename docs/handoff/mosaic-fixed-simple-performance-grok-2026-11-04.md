# Grok bot 交接：固定位置打码简化与合成提速（WIP）

> 用户要求停止当前实现，提交并推送代码与交接文档，由 Grok bot 接手。
> 本提交是未完成的工作快照，不是可发布版本。不要把此前 UI 通过记录视为本次简化后的回归结果。

## 最终需求（以这里为准）

1. 先只实现 **固定矩形位置打码**。
2. 只保留 **模糊、马赛克** 两种效果。
3. **跟随目标暂不实现，后续单独做**；当前 UI 不应启动 SAM/目标跟踪。
4. 简化设置，去掉纯黑、柔边模糊、强度、范围扩大等面向用户的选项。
5. 保留已经实现的删除、时间编辑、草稿保存、生成/替换流程，以及固定区域实时预览、播放跳过删除区间。
6. 合成速度是重点：先消除重复遮罩处理，不用降分辨率/降低画质来代替优化。仍需测最终视频编码耗时。

此前约定仍适用：不增加时间轴缩放、快捷键、波形、形状、旋转、关键帧、转场等功能；不操作生产数据，不自动发布生产。

## 分支与目录

- 分支：`feat/mosaic-timeline-live-preview`
- 本地工作树：`/Users/matao/code/exact-cut-mosaic-editor`
- 仓库：`welltop-cn/exactcut_studio`
- 分支基于 main `9002646`；未合并 main。
- **最后完整通过相关 UI 检查的版本：`0319e1d`**（当时 105 项 UI 检查通过，尚有跟随/多种旧效果能力）。
- 历史功能提交：
  - `f6a084e`：紧凑工作区、时间轴、固定区域实时预览。
  - `a2cc9bc`：删除遮罩直接覆盖缩略图。
  - `e0aea25`：连续密集缩略图、缓存、即时删除跳播。
  - `0319e1d`：去掉更多工具/编辑按钮，窄删除区间隐藏截断文字。
- 当前交接提交在 `0319e1d` 上包含下述 WIP，不要直接部署。
- 原始目录 `/Users/matao/code/exact-cut-fbbm-handoff` 的文档分支及未提交改动属于此前工作，不覆盖它。

## 当前 dev 与任务状态

操作服务前先读 `source/AGENTS.md` 和 `docs/local-dev-services.md`，重新核验 PID/任务。

- 用户围观入口：`http://takeover-m2.localhost:18793/home/#f114b67048c64e63b3a5f64792335fa8`
- 最近核验服务 PID：`89280`，工作目录 `/Users/matao/exactcut-test/takeover-backoff/source`。
- 运行后端还是 `098ffad`，前端已同步到 `0319e1d`。**本次性能和固定模式简化 WIP 均未同步到运行服务，也未重启服务。**
- 后端 `098ffad` 到 main `9002646` 的 source 差异只有测试文件，产品代码一致。
- 数据根：`/Users/matao/exactcut-test/data`。不要删除用户草稿、素材、原生成结果，不要为验收重建数据根。
- 用户这次慢任务已自行完成，没有被中断：
  - batch：`f114b67048c64e63b3a5f64792335fa8`
  - mosaic session：`c147e097eb6305764d6357de779ddd69`
  - job：`59485f9bb36647c591291f5d44bf22d7`
  - 创建：`2026-11-04T11:51:45.606327+00:00`
  - 完成：`2026-11-04T11:59:34.246787+00:00`
  - 观察到的墙钟时间约 **468.64 秒（7 分 49 秒）**，不是已拆分阶段的受控基准。
  - 源：1080×1920、30fps、4500 帧（150 秒），有音频。
  - 删除 `[1552,1702)`；输出 4350 帧，1080×1920，有音频。
  - 两个目标均为 `motion: fixed`，无需 SAM。
- Mac mini SSH：`100.88.167.74`，独立测试树 `/Users/mt/exactcut-test/mosaic-editor-ui`；最近 SSH 多次连接超时。本 WIP 没有第二台复测结果。
- 交接时自建全片基准已失败退出，UI 回归进程也已结束，没有仍需等待的自建测试任务。用户服务保持运行。

## 已证实的慢点

用户截图停在“合并遮罩 … / 4500 帧”。同时观察到主 Python 进程约 100% CPU，而 FFmpeg 几乎空闲；FFmpeg 当时只是接收 `gray 640x1138` 原始遮罩，编码 FFV1 中间文件。

第一目标为固定矩形：

```json
{"start":135,"end":255,"seed_frame":194,"x":160,"y":1570,"w":916,"h":350,"motion":"fixed","effect":{"style":"pixel","strength":"heavy","expansion":"large"}}
```

第二目标：

```json
{"start":442,"end":518,"seed_frame":442,"x":53,"y":398,"w":850,"h":1153,"motion":"fixed","effect":{"style":"pixel","strength":"medium","expansion":"small"}}
```

旧 `mosaic_render.encode_mask` 每个活动帧都调用 `effects.prepare_mask(union_mask(...))`。第一目标 120 帧的矩形完全一样，却执行 120 次大核 `PIL.ImageFilter.MaxFilter`。

同尺寸同第一目标的三次实测：

| 实现 | 每次遮罩准备耗时（秒） |
|---|---|
| 旧 Pillow MaxFilter | 1.211194 / 1.204881 / 1.187406 |
| 当前候选 | 0.005000 / 0.004372 / 0.004203 |

旧 120 次重复约需 **144 秒**，候选相同操作约快 **250 倍**。这是单次遮罩准备的微基准，不是整条合成提速倍数。固定遮罩复用还可进一步去掉重复调用。

## 性能代码现状

### `source/exactcut/mosaic_effects.py`

新增 `maximum_mask(mask, radius)`：

- 利用矩形最大值滤波的可分离性，按水平方向和垂直方向逐次倍增覆盖跨度。
- 用 Pillow `ImageOps.expand`、`ImageChops.lighter` 和 `crop` 计算，避免原 rank filter 大核平方级扫描。
- L 模式遮罩以零填充边缘；输出裁回原大小，不引入新依赖。
- `expand` 和旧 soft_blur 的外扩步骤都调用新方法；GaussianBlur 与效果参数不变。
- 当前测试对随机稀疏灰度、角落像素、不同尺寸/扩大等级，与原 `ImageFilter.MaxFilter` 逐像素比较。

已通过：

```sh
cd /Users/matao/code/exact-cut-mosaic-editor/source
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -q tests/test_mosaic_effects.py -k 'expansion or soft_blur_preserves'
# 18 passed
```

仍建议补充更多边界、软模糊旧效果完整输出一致性；不因 UI 隐藏旧效果就破坏已有 backend 契约。

### `source/exactcut/mosaic_render.py`

`encode_mask` 的候选改动：

- 活动区域排除 failed track。
- 没有活动区域直接写黑遮罩。
- 活动区域全部为固定位置时，只要活动目标索引集合未变，就复用上一张 prepared mask 的 bytes。
- 有动态跟踪区域时仍逐帧计算，不将其误缓存为固定遮罩。
- 只保留一份固定状态缓存，仍逐帧检查取消、写入正确数量的遮罩帧。

新集成测试 `test_fixed_mask_encoding_reuses_only_identical_active_regions`：实际编码/解码 FFV1，在 30000/1001fps 下比较完整 15 帧字节，覆盖 A→A+B→B、空白区间、failed track，验证只准备三种固定遮罩。

最初测试在旧实现上像素相同但调用 15 次，断言失败；加缓存后下述定向命令通过：

```sh
python -m pytest -q -m 'ui or integration' tests/test_mosaic_ui.py tests/test_mosaic_render.py -k 'simple_editor or fixed_mask_encoding'
# 2 passed
```

**尚未跑完整 backend / render 回归。**

## UI 简化 WIP（必须继续审查）

修改了 `source/exactcut/web/index.html` 和 `mosaic.js`：

- 可见效果下拉只剩 `pixel`（马赛克）、`blur`（模糊）。
- 位置选择器换为隐藏的 `mosaicMotion=fixed`。
- 强度/扩大控件暂以 `hidden` 保留 DOM；固定默认值 `medium` / `none`。
- 右侧不再生成动态预览按钮。
- 新手动目标、AI 候选采用、保存目标、render 请求都倾向/显式使用 `motion: fixed`。
- `effectFor()` 将样式收敛为两种，旧 `soft_blur` 映射到 blur，其余非 blur 映射 pixel，并固定 medium/none。
- **`loadDraft()` 当前直接将旧目标在内存中归一为 fixed。旧草稿迁移与已生成结果兼容尚未完成验证。** 不要不加分析就把所有失败当作旧测试过时。
- 旧 effects modal、动态预览/跟踪 handler 等代码仍存在，只是当前入口不再展示。是否清理应按最小实现范围决定。
- `paintLiveControls`、固定实时 canvas、原画对比、删除跳播仍沿用已实现代码。

新测试 `test_simple_editor_exposes_only_fixed_mosaic_and_blur` 通过：只暴露两种效果，无模式/强度/扩大/动态预览入口，提交 fixed + blur/medium/none，且 tracking 能力未开启也能生成。

### 当前 UI 回归失败（9 项）

执行过：

```sh
python -m pytest -q -m ui tests/test_mosaic_ui.py
```

日志在本机：`/Users/matao/exactcut-test/results/mosaic-simple-ui.log`。

失败项：

1. `test_fixed_region_live_preview_changes_pixels_without_render`
2. `test_generated_result_replace_and_compare_use_saved_plan`
3. `test_effect_popup_is_clip_local_and_cancel_discards`
4. `test_clip_effects_are_independent_persist_and_submit`
5. `test_effect_preview_sends_all_clip_regions_without_saving`
6. `test_closing_running_clip_preview_detaches_without_cancelling_or_discarding_effect_draft`
7. `test_ai_region_defaults_to_tracking_and_can_switch_to_fixed`
8. `test_manual_clip_multiple_regions_add_edit_remove_and_preview`
9. `test_saving_one_region_motion_preserves_sibling_default_tracking`

部分用例仍选择已改为 hidden input 的 motion 下拉、使用已移除的效果弹窗入口、或期待默认 tracked，需要按新公开行为重写。

但第 2 项是**已生成结果的替换按钮 disabled**：用例旧目标缺 motion，当前草稿被归一 fixed 后与旧生成计划不一致。必须明确如何处理历史草稿/历史结果，保证不会把不同处理计划的结果误替换，也不能只删除断言。

当前尚未更新 `test_mosaic_tracking_capability_ui.py` 和 `test_mosaic_preview_lifecycle_ui.py` 以适配新范围。保留现有有效的生成、取消、草稿冲突、替换、播放生命周期检查；真正退出产品范围的行为需要明确记录，不能靠放宽断言宣称全通过。

## 全片性能验收：未完成

本机私有脚本：

- `/Users/matao/exactcut-test/bench_mosaic_mask.py`：读取该任务参数，只测遮罩准备。实现前断言 `max(timings)<0.1` 失败，当前通过。
- `/Users/matao/exactcut-test/bench_mosaic_fixed_full.py`：将用户测试源复制到独立目录，冻结相同目标、效果、删除范围，拟以 `encoder_scope(4)` 调用真实 compose，并记录两组 mask 和全片耗时、输出 SHA。
- 输出目录：`/Users/matao/exactcut-test/results/mosaic-fixed-performance/`
- 日志：`/Users/matao/exactcut-test/results/mosaic-fixed-full.log`

第一次全片基准失败：脚本只修改了 Python `sys.path`，子进程 `python -m exactcut.processing_child` 找不到模块，写管道 BrokenPipe，清理还遇到 PermissionError。中间 `combined-mask-0.log` 明确记录 `ModuleNotFoundError: No module named 'exactcut'`。

脚本已补上 `os.environ['PYTHONPATH']` 指向实现 source，但用户要求停工前**没有重跑**，没有有效 `result.json`，不可声称全片完成或输出一致。

上面两份脚本和视频不在 Git；远程 bot 若无法访问本机，需要自行用合成视频建立独立性能用例。真实测试视频与本机用户数据不应上传到仓库。

可在任意 checkout 复现微基准（无需用户素材）：

```sh
cd source
python - <<'PY'
import time
from exactcut import mosaic_render as r, mosaic_effects as e
target={'start':135,'end':255,'x':160,'y':1570,'w':916,'h':350,'motion':'fixed'}
track={'target':target,'result':{'width':1080,'height':1920}}
effect={'style':'pixel','strength':'heavy','expansion':'large'}
for _ in range(3):
    start=time.perf_counter()
    mask=e.prepare_mask(r.union_mask(135,[track],(640,1138)),effect)
    print(time.perf_counter()-start,mask.size)
PY
```

在 `0319e1d` 对照同命令可重现旧 MaxFilter 耗时。计时需记录机器负载，不把微基准外推整片。

## 接手顺序建议

1. 读本文件及现有验证记录，核对本地/远端分支状态。
2. 完成简单固定模式的公开交互与旧草稿处理策略，更新上述失败测试，确认无跟踪请求；固定模糊/马赛克的即时预览和生成都要测。
3. 跑完整相关 effects/render/取消/队列/播放器回归，验证新最大值滤波和固定遮罩复用不改变像素或帧数。
4. 重跑独立全片基准，分开统计 mask、最终 FFmpeg、输出校验；固定源/效果/删除范围/线程，比较像素、音频、帧数及原片不变。当前 250 倍数字仅属于遮罩扩大。
5. 如最终编码仍慢，单独分析 layered graph；当前 pixel 效果可能仍对整片计算多层 scale。此处只是待验证方向，不能直接更改编码策略或宣称瓶颈已证实。
6. 只有新代码验证完成且用户 dev 没有活动任务时，才按服务清单更新本机后端与前端、正常重启并核验。本次 WIP 没有做这一步。
7. 不自动合 main 或部署生产；将后续实际结果追加到验证文档。

## 相关资料

- `docs/verification/mosaic-timeline-live-preview-2026-11-04.md`：此前 UI 变更与验收历史，不代表当前 WIP 全通过。
- `docs/local-dev-services.md`：服务/工作树/数据边界。
- 本机 runbook：`/Users/matao/code/troubleshooting-runbooks/operations/exactcut-long-render-performance.md`。
- 本机 runbook：`/Users/matao/code/troubleshooting-runbooks/runbooks/exactcut/mosaic-mask-timebase.md`。
- 本机 Python 约定：`/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。
