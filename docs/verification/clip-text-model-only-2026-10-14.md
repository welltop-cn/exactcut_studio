# 粘贴文本统一模型识别

日期：2026-10-14。本地开发仓库修改，未部署。

## 复现与变更

输入 `EP4: 00:49:00-02:16:18,, EP5: 00:00:01-01:08:13`，原实现返回“时间单位需要澄清”，模型调用次数为0。原因是 `_explicit_frame_text` 的四段时间码正则从集号数字开始匹配，且允许冒号两侧空白。

按用户要求，移除本地时间文法解析和单位预拦截，帧/毫秒文本统一通过现有 `interpret` 识别。帧提示词明确集号冒号、一行多段及重复逗号的含义，仍要求只提取分秒帧、不换算帧率，单位不清返回 issue。

保留输入大小限制、鉴权、AI 开关和并发限制、严格结构化输出、数字范围、原文行覆盖顺序、起止顺序、段数上限和全有或全无填入。所有文本识别现在依赖 AI 可用。

## 验证

- 新增7个路由回归先在旧实现下失败：标准格式、截图原文、全角分隔符及4类单位疑问均未到达模型。
- 修改后执行：

```sh
cd source
PYTHONPATH=. /Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest tests/test_clip_text_frames.py tests/test_clip_text.py tests/test_llm_intake.py tests/test_source_frame_render_integration.py tests/test_clip_text_ui.py
```

结果：63 passed in 16.57s。覆盖接口、失败释放并发槽、数值范围、倒序和单帧、段数、页面追加/替换/撤销及真实本地视频帧边界与渲染。

模型响应使用测试替身/MockTransport，未调用真实外部模型；因此验证的是程序路由、结果处理及下游帧正确性，不是模型识别准确率。未执行发布前全量测试，未重启开发服务，未部署生产。
