"""User actions alongside redacted technical evidence; no automatic retries."""
import os
import re
from urllib.parse import urlsplit, urlunsplit


def redact(value, *, limit=4000):
    text = str(value or '')
    for key, secret in os.environ.items():
        if len(secret) >= 8 and re.search(r'(TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY)', key, re.I):
            text = text.replace(secret, '[已隐藏]')
    text = re.sub(r'(?i)(Bearer\s+)\S+', r'\1[已隐藏]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[已隐藏]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|password|secret|authorization)\s*[=:]\s*)[^\s,;]+', r'\1[已隐藏]', text)
    def safe_url(match):
        try:
            url = urlsplit(match[0])
            # Error URLs are diagnostic only; query strings may contain signed credentials.
            return urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path,
                               '[已隐藏]' if url.query else '', ''))
        except ValueError:
            return '[链接已隐藏]'
    return re.sub(r'https?://[^\s<>\"\']+', safe_url, text)[:limit]


def guidance(message, stage=None, *, continuation=False):
    if not message:
        return None
    lower = str(message).lower()
    code, title, action = 'unknown', '本次执行未完成', '查看技术详情；制作失败可重试，需修改内容时请创建新草稿。'
    if stage == 'fbbm' and str(message).startswith('状态查询暂时失败，将自动重试：'):
        code, title, action = 'delivery_read_retry', '状态查询暂时不可用', '系统将自动查询原交付记录，无需重新上传；超过查询期限后会提示人工核实。'
    elif stage == 'fbbm' and str(message).startswith('待核实：'):
        code, title, action = 'delivery_unverified', '交付结果待核实', '请联系管理员核实原交付记录；已有成片可继续播放和下载。'
    elif any(s in lower for s in ('空间', 'no space', 'disk full')):
        code, title, action = 'disk_space', '可用磁盘空间不足', '在空间管理中预览并清理可回收缓存，或释放磁盘空间后重试。'
    elif any(s in lower for s in ('已变化', 'sha256', 'hash mismatch')):
        code, title, action = 'integrity', '文件版本发生变化', '重新预览并确认当前成片；不要沿用旧版本确认。'
    elif continuation:
        code, title, action = 'fbbm_continue', 'FBBM 尚未就绪', '点击“继续查询 FBBM”沿用原记录；如平台报告失败，先查看平台原因。'
    elif stage == 'preview':
        code, title, action = 'preview', '轻量预览未生成', '可先查看正式成片，也可点击“生成轻量预览”重试。'
    elif 'title card text is missing or unreadable' in lower:
        code, title, action = 'qc', '字卡文字未正常显示', '检查字卡文字及字幕位置，避免遮挡；修改后重新制作，如仍失败请查看技术详情。'
    elif 'first frame is black' in lower:
        code, title, action = 'qc', '成片开头画面过暗或疑似黑屏', '检查开头画面和剪辑起点；若原片开头为黑屏，请调整起点后重新制作。'
    elif 'cannot extract first frame' in lower:
        code, title, action = 'qc', '无法读取成片开头画面', '查看技术详情并重试制作；检查原片及编码是否完整。'
    elif any(s in lower for s in ('时长不符', 'qc未通过', '时长不一致')):
        code, title, action = 'qc', '成片时长或格式校验未通过', '查看时长校对及技术详情；检查原片，必要时修改时间线后重新制作。'
    elif any(s in lower for s in ('缺少', '正片', '尾帧', 'decode', 'moov atom')):
        code, title, action = 'source', '原片缺失或无法完整读取', '检查所需集数和原片完整性，再重试获取与制作。'
    elif any(s in lower for s in ('时间线', 'end_ms', 'start_ms', '时间范围')):
        code, title, action = 'timeline', '剪辑时间需要校对', '对照 Excel 解析说明和原片时长，修改为有效范围后重新制作。'
    elif stage == 'ai_emphasis' or any(s in lower for s in ('openai', '语义', '429', 'api key')):
        code, title, action = 'ai', 'AI 重点分析未完成', '服务恢复后重试，或在新草稿中改为手动重点／不强调。'
    elif any(s in lower for s in ('hook', '排版', '字幕')):
        code, title, action = 'hook', 'Hook 排版或画面分析未完成', '在新草稿中缩短文案、调整位置或手动重点，并重新预览确认。'
    elif stage == 'upload' or any(s in lower for s in ('oss', '上传', 'network', 'connection', 'timeout')):
        code, title, action = 'network', '上传或网络请求未完成', '检查网络及服务日志后重试；已保存的平台记录会继续使用。'
    elif any(s in lower for s in ('中断', '启动失败', '进程已结束')):
        code, title, action = 'interrupted', '执行进程中断', '查看服务日志后重试，已校验成片及已保存上传记录会复用。'
    return {'code': code, 'title': title, 'action': action, 'detail': redact(message)}
