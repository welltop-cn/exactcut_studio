import pytest
from PIL import Image
from exactcut import cutting
from exactcut import mosaic_effects as effects


def test_effect_contract_and_mask_expansion():
    assert effects.validate(None)=={'style':'pixel','strength':'medium','expansion':'small'}
    for bad in [False,[],{'style':'unknown'},{'expansion':20},{'extra':1}]:
        with pytest.raises(cutting.CutError):effects.validate(bad)
    mask=Image.new('L',(100,100));mask.putpixel((50,50),255)
    assert effects.expand(mask,'none').getbbox()==(50,50,51,51)
    assert effects.expand(mask,'large').getbbox()[0]<effects.expand(mask,'small').getbbox()[0]
    assert effects.expand(Image.new('L',(100,100)),'large').getbbox() is None


@pytest.mark.parametrize('style',['pixel','blur','soft_blur','solid'])
def test_real_ffmpeg_effects(style,tmp_path):
    from test_cutting import make_video
    from exactcut import mosaic_render as render
    source=tmp_path/'source.mp4';make_video(source,frames=5)
    meta=cutting.probe_source(source)
    masks=tmp_path/'track'/'masks';masks.mkdir(parents=True)
    for i in range(5):Image.new('L',(meta['width'],meta['height']),255).save(masks/f'{i:06d}.png')
    target={'start':0,'end':5}
    track={'target':target,'directory':masks.parent,'result':{'width':meta['width'],'height':meta['height'],'frames':[{'box':[0,0,meta['width'],meta['height']]}]*5}}
    output=tmp_path/'out.mp4'
    render.compose(source,output,meta,[track],effect={'style':style,'strength':'heavy','expansion':'none'})
    assert cutting.probe_source(output)==meta
    if style=='solid':
        frame=cutting.run(['ffmpeg','-v','error','-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])
        assert max(frame)<5


def test_effect_changes_job_identity_and_preview_uses_same_graph(tmp_path):
    from exactcut import mosaic_tracking as tracking
    session={'source_sha256':'a'*64};target={'start':2,'end':5}
    assert tracking.identity(session,target,{})!=tracking.identity(session,target,{}, {'style':'solid'})
    from test_cutting import make_video
    source=tmp_path/'source.mp4';make_video(source,frames=8)
    meta=cutting.probe_source(source)
    mask=Image.new('L',(meta['width'],meta['height']),255)
    for i in range(3):mask.save(tmp_path/f'{i:06d}.png')
    output=tmp_path/'preview.mp4'
    cutting.run(['ffmpeg','-y','-v','error','-i',str(source),'-framerate','30','-i',str(tmp_path/'%06d.png'),'-filter_complex',effects.graph(meta,{'style':'solid'},2,5),'-map','[out]','-an','-r','30','-fps_mode','cfr','-c:v','libx264','-pix_fmt','yuv420p',str(output)])
    assert cutting.probe_source(output)['frame_count']==3
    pixels=cutting.run(['ffmpeg','-v','error','-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])
    assert max(pixels)<5


def test_soft_blur_preserves_core_and_feathers_only_outside():
    mask=Image.new('L',(200,200))
    mask.paste(255,(70,70,130,130))
    soft=effects.prepare_mask(mask,{'style':'soft_blur','expansion':'none'})
    assert soft.getpixel((70,100))==255
    assert 0<soft.getpixel((67,100))<255
    assert soft.getpixel((0,0))==0
    assert effects.prepare_mask(Image.new('L',mask.size),{'style':'soft_blur'}).getbbox() is None
    for style in ('pixel','blur','solid'):
        assert effects.prepare_mask(mask,{'style':style,'expansion':'none'}).tobytes()==mask.tobytes()
