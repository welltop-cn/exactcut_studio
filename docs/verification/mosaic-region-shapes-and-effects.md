# 打码效果与区域形状验收

## 本轮范围

- 添加片段时，处理方式为“打码”才显示“打码效果”：马赛克、模糊；切换为删除时隐藏，切回时保留本次选择。
- 所选效果随片段保存，并用于最终生成。
- 区域支持矩形、圆形、椭圆形，每个区域独立保存形状。圆形绘制、拖动缩放和像素宽高编辑保持等宽等高。
- 实时画布和最终固定位置遮罩按所选形状处理；不带 shape 的既有区域仍按矩形处理。
- 界面隐藏“识别当前帧”和“对比原画”入口，保留现有内部实现。

## 数据契约

`mosaic_tracking.validate_target` 接受可选 `shape`，取值为 `rectangle`、`circle`、`ellipse`。省略时按矩形解释；圆形要求 `w == h`，非矩形仅用于固定位置。沿用 schema_version=1，无需迁移旧草稿。

## 验证

使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`：

- `tests/test_mosaic_ui.py` 全部用例分成两组执行：71 passed / 2 skipped，81 passed。
- `tests/test_mosaic_render.py` 集成层：42 passed。
- `test_editor_layout.py`、`test_mosaic_render.py`、`test_mosaic_tracking.py`、`test_mosaic_effects.py` 默认单元层：60 passed。
- 真实 FFmpeg 视频检查三种形状：内部黑色遮罩、圆/椭圆角部保留原画、区域外保留原画、时间区间前后帧保留原画。
- `git diff --check` 通过。

全量 UI 回归后发现共享工作区同时出现片段勾选界面的其他改动；本轮未撤销或提交那些改动。最终另跑本轮定向用例，8 passed（147 deselected），核对兼容。

## 本地验收实例

- 入口：`http://127.0.0.1:18794/home/`。
- 更新前核对进程 cwd 为本仓库 `source`，数据根 `.web-state` 无运行/排队记录，未发现 FFmpeg/FFprobe 媒体进程。
- 使用现有 `/Users/matao/exactcut-test/start.sh` 更新服务，旧 PID 73558 正常退出，新 PID 59992 监听 18794，cwd 仍为本仓库 `source`。
- `/healthz` 返回 HTTP 200、status=ok、worker_alive=true。没有执行生产发布。
- 登录页阻止匿名 HTTP 读取界面资源；浏览器控制出现标签会话不一致，因此未将实际登录页面的可视操作计为验收通过。界面行为证据来自隔离 Playwright 测试，渲染证据来自合成视频测试。
