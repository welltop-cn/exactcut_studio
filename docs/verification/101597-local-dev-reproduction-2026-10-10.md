# 101597 反馈本地复现（2026-10-10）

环境：source/bin/dev-web，127.0.0.1:8793，source/.venv Python 3.11.15。独立开发数据，禁用外部下载、AI 和发布。未修改业务代码。

## HTTP 实测

通过源码定义的登录、blank、save、start、clone 接口测试。素材为自行生成的蓝底音视频，测试剧号 989597，出表人「复现101597」。未取得原始 101597.xlsx；四条历史文案取自 tests/test_phase1_hook.py。

- 四条历史文案均通过 save 校验，start 返回 202，完成 Hook 渲染后到达 QC。
- 150 个 word 的极长文案：save 返回文案过长错误；start 返回 400；状态保留 draft。原项目缩短文案后 save 返回 200，errors 为空。
- 引用不存在的独立 EP2 触发真实 worker failure：3051107a9e704425ad33582596cb5ffb。
- 对失败项目 clone purpose=repair 返回 201；新草稿 3d050e8c9a874daa8d40289d37509b9a。序号由 6 变为 7；改为可用 EP1 后 save=200，start=202，没有命名冲突。
- 对历史文案 partial 批次也成功复制并重新提交。20 秒四文案批次：8cb592dd2b524a9cb53c4ede5d4c4963。
- 当前前端 app.js controls 定义 failed/partial/interrupted/review 时展示「修改后重新制作」，点击调用 clone purpose=repair。浏览器登录未完成，按钮实际可见性未完成 UI 验收；以上流程结论来自真实 HTTP 请求和前端源码。

## 额外发现与边界

合成素材在带 Hook 输出的最终 QC 失败：3 秒 fps=30.341，10 秒 fps=30.101，20 秒 fps=30.050。20 秒仅使用短文案 She returned. 的对照批次 d57ea417a43c4ddaba83badad0cd98cf 同样 fps=30.050。因此不能把该错误归因为长文案；尚未完成此独立帧率问题的根因定位，不能宣称端到端制作成功。

开发环境关闭 AI，未覆盖 AI Hook 调用及原始 Excel/真实原片路径。

## 回归检查

source/.venv/bin/python -m pytest tests/test_phase1_hook.py tests/test_workflow.py -q

退出码 0，46 项通过。
