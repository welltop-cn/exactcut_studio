# 同项目多素材并发实施记录

规范：`intra-project-concurrency-plan.md`；基线 `3509a4f`，数据 schema 1。

## 环境与隔离

2026-10-10，macOS 15.8.1 arm64，Python 3.12.12（conda-forge）。全部 Python 使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。

在 `source/` 执行 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-review-tests.py`：包装器为每次运行建立独立 `TemporaryDirectory` 数据根，设置 `EXACTCUT_ENVIRONMENT=development`，清除 EXACTCUT/OPENAI/ALIYUN/FEISHU/CF_ADS/GEMINI/GOOGLE_API 凭据变量；调用 `python -m pytest -o addopts= -q`。外部客户端均模拟，无真实上传。基线 **676 passed，97.59s**。

按组执行 RED → 最小实现 → GREEN → 审查，最后完整 pytest 与已提交版本干净 clone 验证。保留用户原有两个文档和三个前端文件的未提交改动；只按路径暂存自己文件。不部署、不触碰生产。

## 组 1：同剧锁收紧（测试先行）

- `test_intra_project_locks.py`：先确认 **7 failed，4.37s**，覆盖共享读取时缓存命中、忙锁立即等待、同选段不同 Hook 并行、采样锁外、私有副本抵抗缓存清理。
- 最小实现：缓存先判断就绪；缺失才尝试非阻塞独占锁；锁忙抛 `ResourceDeferred`。基底检查/生成/发布及复制到请求私有工作文件在锁内，采样和 Hook 在锁外。
- 规范审查指出无锁快路径遇到清理时的 `is_file/stat` 竞态。先补确定性删除测试，确认 **1 failed**（FileNotFoundError），再将消失视为未命中并在锁内重新检查。
- GREEN：锁/正片缓存/Hook/磁盘/纯剪/强调回归 **74 passed，9.88s**；规范复核 **8 passed**；独立质量审查 **38 passed，6.46s**，通过。
- 预取流程的等待异常传播随阶段集成组补齐，避免转换成永久失败。

## 后续分组

2. 每素材结果存储、旧整批兼容、执行器/进度隔离；素材输入、产物证据和磁盘预算隔离。
3. request 围栏、fetch 扇出、render 单项目容量、恢复/核实/确认隔离；生产模板 render=4、per_project=2。
4. 完整集成与兼容回退验证、干净 clone、Linux 验收说明。

## 回退

设置 `concurrency.per_project=1` 降低单项目 render 并发；设置 `concurrency.enabled=false` 全局保守串行。无需还原数据，新代码合并读取旧 `production/results.json` 与每素材文件。配置降低时已驻留（含暂停）进程仍计入容量，不再准入超额工作；应先排空再观察串行回退。若回退到不识别阶段/分片的旧代码，必须遵守已有 release 管理器的阶段排空守卫，不直接切换。
