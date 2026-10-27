## 审查结论

**不通过（需修改后复审）。** 核心竞态场景（seek 触发 pause → play Promise 以 AbortError 拒绝 → 后续 playing 仍残留错误）已经被 attempt/epoch/context 机制覆盖；但按题述 scope，仍有 2 个实质性缺口，其中至少 1 个可直接归为阻塞项。

本次仅做静态审查，未运行测试、未改文件。

---

## 已确认正确的部分

- 显式暂停路径大多已统一走 `pausePlayback()`：seek、播放/暂停切换、打开、关闭、提交剪切、结果切换等。
- 拒绝回调始终先调用 `reportMediaPlayFailure`，即使 UI 被抑制也保留诊断，符合“stale rejects stay diagnostic”。
- `playing` 只清除 `playbackError` 为真的播放错误，输入校验/其他状态会先重置该标志，因此“新校验消息不被 playing 清掉”基本成立。
- 没有按错误名称直接吞掉 AbortError；仍保留了“有效失败可见”的意图。
- 未改动后端、Range、帧并发、409 版本校验。

---

## 问题

### 1. 阻塞：打码结果页的正常完成路径没有调用 `pausePlayback()`

文件/行：
- `source/exactcut/web/mosaic.js:640-641`：开始生成时先 `batchJob=null`，再 `showResult(true)`
- `source/exactcut/web/mosaic.js:647-649`：`showResult(show)` 只在 `show && batchJob?.status==='completed'` 时暂停
- `source/exactcut/web/mosaic.js:568-573, 632`：轮询到完成后只调用 `paintBatch()`，结果页通过 `showOutput=resultMode && completed` 直接显示，没有再次经过 `showResult(true)`

后果：
- 生成开始时 `batchJob` 为 null，`showResult(true)` 不会暂停原片。
- 生成完成、切到结果页时也没有 `pausePlayback()`。
- 若原片正在播放或 play Promise 仍 pending，进入结果页不会使该 attempt 失效；原片音频可能继续在结果页背后播放，迟到的 AbortError 也可能在返回工作区后落到旧状态上。
- 这与 scope 中“result view invalidate pending playback attempts”直接冲突；剪切侧 `setResult(true)` 已经无条件 `pausePlayback()`（`cutting.js:105-110`），两侧行为不一致。

建议：
- 进入结果态时无条件暂停原片（至少在生成开始和生成完成两个时点保证），而不是依赖 `batchJob?.status==='completed'` 的调用时机。
- 增加打码“生成中播放 → 完成后进入结果页”的回归测试。

---

### 2. 阻塞/高风险：`statusRevision` 过宽，会吞掉仍然有效的播放失败

文件/行：
- `source/exactcut/web/cutting.js:40-44`、`source/exactcut/web/mosaic.js:57-61`
- `status()` 每次调用都 `statusRevision++`：`cutting.js:36`、`mosaic.js:67`

当前判断：

```js
messageVersion !== statusRevision
```

只要 play 开始后、Promise 拒绝前发生了**任何** `status()`，当前 attempt 即使没有被 pause/seek/close/open 取消，真实失败也会被静默丢弃。

可触发场景：
- 打码自动识别/扫描状态更新：`mosaic.js:254,261,282,320-331`
- 剪切生成轮询状态：`cutting.js:223-224`
- 用户在校验/操作过程中产生的新状态：如 `cutting.js:338-341` 等
- 其他非播放状态更新

这会把“主动取消的旧请求”和“仍在进行中的播放失败”混为一谈。题述要求“active failures including unexpected AbortError must still show”，而这里不仅 AbortError，NotSupportedError、NotAllowedError 也都会在某些状态更新后消失。

现有测试只覆盖“先失败，再产生新校验消息”（`test_editor_playback.py:61-72`），没有覆盖“play pending 期间先产生扫描/校验/过程状态，再拒绝”的顺序，因此测试会通过但缺口仍在。

建议：
- 用单独的“播放状态所有权”或明确的操作取消 token 判断是否抑制；不要把任何 `status()` 都视为播放取消。
- 或者明确产品契约：如果“任何更新的状态都优先于播放失败”，则必须补上 pending 期间状态更新的测试，并修正 scope 表述。

---

### 3. 中低风险：`open()` 的暂停发生在 epoch/context 重置之前，旧 `pause` 事件未绑定 attempt

文件/行：
- `source/exactcut/web/cutting.js:257-259`：`pausePlayback()` 在 `++epoch`、`context=null` 之前
- `source/exactcut/web/cutting.js:327`：`video.onpause` 未检查 `playAttempt`/`epoch`
- `source/exactcut/web/mosaic.js:349-350`：同样是先 pause，后重置 epoch/context
- `source/exactcut/web/mosaic.js:818`：`video.onpause` 只改按钮文案

分析：
- 对“旧 play Promise 拒绝”是安全的：`pausePlayback()` 先递增 `playAttempt`，旧回调会被 `attempt!==playAttempt` 拦住。
- 对“旧 pause 事件”则没有专门 token。原生 `pause` 事件通常异步派发，正常会在 `open()` 的 `await api(...)` 期间、`context` 仍为 null 时执行，因此通常无害。
- 但在同步派发 pause 的测试桩、重入/缓存型 open、或异常任务顺序下，旧 pause 事件可能在新的 `context`/`video.src` 安装后执行。剪切侧 `onpause` 会据此重新计算 `wanted` 并设置 `seekTimer` 调度 `loadFrame`，可能覆盖新会话的初始定位；打码侧至少会错误重置播放按钮文案。
- 当前测试的 `video.pause` 桩只设置 `paused=true`，不派发 `pause` 事件（`test_editor_playback.py:20-23`），所以没有覆盖这一风险。

建议：
- 把 open 时的暂停放到 epoch/context 重置之后，或用 session/generation token 保护 `onpause` 的副作用；并补一个“关闭/重开期间异步派发 pause 事件”的测试。

---

### 4. 测试覆盖缺口

`source/tests/test_editor_playback.py`：
- `cancel` 参数只有 `seek/pause/close/reopen`（约第 30-46 行），**没有 result view**。
- 没有“play pending 期间先发生状态更新，再拒绝”的测试。
- 没有实际派发 `pause` 事件，无法覆盖 open 时旧 pause 事件的顺序风险。
- 文档 `docs/verification/playback-fix-2026-10-27.md:13` 声称新增 20 项，但当前文件按参数组合计算是 18 项；证据数量与文档不一致。

这些不是产品逻辑缺陷本身，但会掩盖上面的回归缺口，建议补齐后再复核验收证据。

---

### 5. 可选问题

- `playSource()` 开始新播放尝试时不会清除上一次仍显示的播放错误；用户点击重试后，在成功 `playing` 前旧错误仍然显示。不是本次 scope 的硬要求，但 UX 上可考虑在开始新 attempt 时清掉“播放所有”的旧状态。
- 诊断上报放在 stale 判断之前是正确的；但 `reportMediaPlayFailure` 若未来实现抛异常，会在 UI guard 之前中断 catch。当前 `diagnostics.js` 内部已 try/catch，生产路径安全，属于防御性建议。

---

## 最终判定

**核心竞态修复方向正确，但按给定 scope 仍不通过。**

- **阻塞项**：打码结果完成路径没有使 pending play 失效（问题 1）；`statusRevision` 会吞掉仍然有效的播放失败（问题 2，若产品明确接受“任何新状态优先”则可降级，但必须写清契约并补测试）。
- **非阻塞但建议修复**：open 时 pause 与旧 pause 事件未绑定 attempt（问题 3）。
- **测试/证据需补齐**：result view、pending 期间状态更新、真实 pause 事件，以及文档声称的测试数量。
