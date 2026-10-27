## 复审结论

**通过（静态复审）。** 首轮的 3 个实质问题均已按修正说明落实，未发现新的确定性阻塞项或高风险回归。

---

## 旧问题核实

### 1. 打码结果完成路径未暂停 —— 已解决

- `source/exactcut/web/mosaic.js:652-655`：`showResult(true)` 现在无条件调用 `pausePlayback()`。
- `source/exactcut/web/mosaic.js:572-573`：`paintBatch()` 在首次进入已完成结果页时，即 workspace 仍可见但 `showOutput` 已为真时，再调用一次 `pausePlayback()`，覆盖“生成期间开始播放，完成后进入结果页”的时序。
- `source/exactcut/web/mosaic.js:645-646`：生成开始时先 `showResult(true)`，再启动请求。
- 对应测试 `test_editor_playback.py:110-142` 覆盖了生成开始、生成中播放、完成后结果页进入及迟到拒绝。
- 剪切侧 `cutting.js:342` 提交时暂停，`showJob()` 经 `setResult(true)` 暂停；`test_editor_playback.py:144-` 覆盖渲染结果路径。

### 2. `statusRevision` 吞掉有效播放失败 —— 已解决

- 两个文件中均已无 `statusRevision` / `messageVersion`。
- `cutting.js:9,36-54`、`mosaic.js:11,53-72`：workflow 的 `statusMessage/statusError` 与 `playbackError` 独立存储。
- `playing` 只清除 `playbackError`，保留 workflow 消息和错误标志。
- `paintStatus()` 同时展示 workflow 消息与当前播放失败。
- 测试 `test_editor_playback.py:62-96` 覆盖两种顺序：失败后出现校验消息、pending 期间出现校验消息再失败；也验证成功 `playing` 后只清除播放错误。

### 3. open 暂停顺序与迟到 pause 事件 —— 已解决

- `cutting.js:263`：先 `++epoch`、`context=null`，再 `pausePlayback()`。
- `mosaic.js:354`：同样先重置 epoch/context，再暂停。
- `cutting.js:331`、`mosaic.js:823`：`onpause` 在 `video.paused === false` 时直接返回，不再覆盖新播放的按钮状态。
- 测试 `test_editor_playback.py:98-108` 覆盖关闭/重开后再播放，并派发迟到 `pause` 事件。

### 4. 测试数量与证据 —— 已一致

当前 `source/tests/test_editor_playback.py` 按参数展开实际为：

- 4 种取消路径 × 2 个编辑器 = 8
- 3 种失败名称 × 2 个编辑器 = 6
- 其余 5 个双编辑器用例 × 2 = 10
- 打码生成结果 1 项
- 剪切渲染结果 1 项

合计 **24 项**，与文档 `docs/verification/playback-fix-2026-10-27.md:13-17` 的 24 项、142 项相关测试总数一致。

---

## 新的回归检查

未发现新的确定性缺陷：

- 未发现仍可直接调用原片 `video.pause()` 而绕过 `playAttempt` 的路径。
- 未发现状态更新会无条件抑制当前播放失败的路径。
- `showResult(true)` 的额外暂停不会影响返回工作区；`showResult(false)` 只暂停结果视频。
- `paintBatch()` 的结果页暂停有 workspace 可见性保护，不会在已经处于结果页时反复产生副作用。
- `playing` 清除播放错误时不会清除 workflow 错误，`statusError` 与 `playbackError` 的 class 合并逻辑正确。

## 残余非阻塞观察

`playbackError` 只在成功播放或重新打开时清除；暂停、拖动、取消请求后旧播放失败文案可能继续保留，直到下一次成功播放。该行为与本次明确的“playing clears only playbackError”契约一致，未判为缺陷。

## 最终判定

**通过。** 首轮阻塞项均已解决，24 项新增测试覆盖了修正点；未发现需要继续阻塞合入的具体缺陷。
