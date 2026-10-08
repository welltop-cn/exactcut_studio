"""Disposable streaming derivatives; publishing always uses the checked master."""
from pathlib import Path
import os
import time
from .media import run_checked, ffprobe
from .util import file_sha256


def make_preview(master: Path, output: Path, master_sha256: str) -> dict:
    if file_sha256(master) != master_sha256:
        raise ValueError('正式成片版本已变化，未生成预览')
    started=time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary=output.with_name(output.stem+'.part.mp4')
    try:
        run_checked(['ffmpeg','-y','-v','error','-i',str(master),'-map','0:v:0','-map','0:a:0?',
                     '-vf',"scale=w='min(540,iw)':h=-2,fps=24",'-c:v','libx264','-preset','veryfast',
                     '-crf','30','-pix_fmt','yuv420p','-c:a','aac','-b:a','64k','-movflags','+faststart',str(temporary)],timeout=600)
        original=ffprobe(master); info=ffprobe(temporary)
        expected=float(original['format']['duration']);actual=float(info['format']['duration'])
        if abs(expected-actual)>.15 or not any(s.get('codec_type')=='video' for s in info['streams']):
            raise ValueError('预览时长或视频检查失败，继续使用正式成片预览')
        if file_sha256(master)!=master_sha256:
            raise ValueError('正式成片版本已变化，未保存预览')
        os.replace(temporary,output)
        return {'master_sha256':master_sha256,'sha256':file_sha256(output),
                'bytes':output.stat().st_size,'master_bytes':master.stat().st_size,
                'duration_ms':round(actual*1000),'elapsed_seconds':round(time.monotonic()-started,3)}
    finally:
        temporary.unlink(missing_ok=True)


def available_preview(path, request, record):
    metadata=record.get('preview') or {}
    file=path/'production/previews'/request.output_filename
    if metadata.get('master_sha256')==record.get('output_sha256') and file.is_file():
        return file
    return None
