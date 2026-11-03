"""Only the bottom subtitle region may reach the recognition model."""
import json
import subprocess

import pytest

from exactcut import preview_subtitle_model as model, preview_subtitles as subtitles
from exactcut.media import ffprobe
from exactcut.util import file_sha256


@pytest.mark.integration
@pytest.mark.parametrize('height',[80,82,1080])
def test_proxy_excludes_the_upper_picture_and_keeps_source_unchanged(tmp_path,height):
    source=tmp_path/'source.mp4';proxy=tmp_path/'proxy.mp4'
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i',f'color=c=red:s=160x{height}:r=4:d=1',
                    '-vf','drawbox=x=0:y=ih/2:w=iw:h=ih/2:color=blue:t=fill',
                    '-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True)
    before=file_sha256(source)
    cfg=subtitles.settings();cfg.update(proxy_long_edge=1920,sample_fps=4)
    model.prepare_chunk(source,proxy,0,1000,cfg)
    video=next(s for s in ffprobe(proxy)['streams'] if s['codec_type']=='video')
    assert video['width']==160 and video['height']==height//4*2
    pixel=subprocess.check_output(['ffmpeg','-v','error','-i',str(proxy),'-frames:v','1','-vf','crop=2:2:0:0',
                                   '-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
    # Red announcement area is absent; even the first output pixel is from below.
    assert pixel[0]<40 and pixel[2]>200
    assert video['avg_frame_rate']=='4/1'
    assert file_sha256(source)==before


def test_bottom_region_is_explicitly_bound_into_recognition_cache_identity():
    cfg=subtitles.settings();policy=model.fingerprint(cfg)
    assert policy['engine']>=4
    assert policy['region']['name']=='lower_half'
    assert policy['region']['filter'].startswith('crop=')


def test_bottom_only_policy_still_rejects_overlapping_invalid_model_output():
    cues=[{'start_ms':100,'end_ms':500,'original_text':'A','translation_zh':'甲'},
          {'start_ms':400,'end_ms':600,'original_text':'B','translation_zh':'乙'}]
    with pytest.raises(ValueError,match='重叠'):
        model.checked_cues(json.dumps({'cues':cues}),1000)
