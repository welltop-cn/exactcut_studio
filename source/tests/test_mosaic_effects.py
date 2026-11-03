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


@pytest.mark.parametrize('strength,block,sigma',[('light',16,8),('medium',30,16),('heavy',50,28)])
@pytest.mark.parametrize('width,height',[(360,640),(1080,1920),(1920,1080),(180,320)])
def test_effect_strength_scales_with_video_long_edge(strength,block,sigma,width,height):
    meta={'width':width,'height':height}
    ratio=max(width,height)/640
    size=max(1,round(block*ratio))
    assert effects.transform(meta,{'strength':strength})==f'scale={max(1,width//size)}:{max(1,height//size)}:flags=area,scale={width}:{height}:flags=neighbor'
    for style in ('blur','soft_blur'):
        assert effects.transform(meta,{'style':style,'strength':strength})==f'gblur=sigma={sigma*ratio:g}:steps=2'

def detail_source(path,frames=8,fps='30'):
    # Fine spatial detail so a real gblur is visible in decoded pixels.
    cutting.run(['ffmpeg','-y','-v','error','-f','lavfi','-i',f'testsrc2=size=112x64:rate={fps}',
                 '-frames:v',str(frames),'-c:v','libx264','-pix_fmt','yuv420p',str(path)])
    return path


def mask_video(path,meta,white):
    import subprocess
    from exactcut.media import bounded_ffmpeg
    raw=b''.join(bytes([255 if n in white else 0])*(meta['width']*meta['height'])
                 for n in range(meta['frame_count']))
    subprocess.run(bounded_ffmpeg(['ffmpeg','-y','-v','error','-f','rawvideo','-pix_fmt','gray',
                    '-s',f"{meta['width']}x{meta['height']}",'-r',f"{meta['fps_num']}/{meta['fps_den']}",
                    '-i','pipe:0','-c:v','ffv1',str(path)]),input=raw,capture_output=True,check=True)
    return path


def decode_frames(path,meta):
    raw=cutting.run(['ffmpeg','-v','error','-i',str(path),'-f','rawvideo','-pix_fmt','rgb24','-'])
    size=meta['width']*meta['height']*3
    return [raw[i:i+size] for i in range(0,len(raw),size)]


def render_layered(path,source,masks,graph,meta):
    cutting.run(['ffmpeg','-y','-v','error','-i',str(source),*[arg for mask in masks for arg in ('-i',str(mask))],
                 '-filter_complex',graph,'-map','[out]','-an','-c:v','ffv1',str(path)])
    return decode_frames(path,meta)


@pytest.mark.parametrize('style',['blur','soft_blur'])
def test_layered_graph_blur_timeline_skips_frames_outside_source_intervals(style,tmp_path):
    source=detail_source(tmp_path/'source.mp4');meta=cutting.probe_source(source)
    mask=mask_video(tmp_path/'mask.mkv',meta,set(range(meta['frame_count'])))
    appearance={'style':style,'strength':'heavy','expansion':'none'}
    old=render_layered(tmp_path/'old.mkv',source,[mask],effects.layered_graph(meta,[appearance]),meta)
    active=render_layered(tmp_path/'active.mkv',source,[mask],effects.layered_graph(meta,[appearance],[[(2,4)]]),meta)
    off=render_layered(tmp_path/'off.mkv',source,[mask],effects.layered_graph(meta,[appearance],[[]]),meta)
    assert len(old)==len(active)==len(off)==meta['frame_count']
    for frame in range(meta['frame_count']):
        if 2<=frame<4:
            assert active[frame]==old[frame]
            assert off[frame]!=old[frame]
        else:
            assert active[frame]!=old[frame],'gblur must be skipped outside the active interval'
            assert active[frame]==off[frame],'inactive frames must stay pass-through'


def test_layered_graph_intervals_keep_every_masked_frame_identical(tmp_path):
    source=detail_source(tmp_path/'source.mp4');meta=cutting.probe_source(source)
    first=mask_video(tmp_path/'first.mkv',meta,{1,2,5})
    second=mask_video(tmp_path/'second.mkv',meta,{3,4,6,7})
    appearances=[{'style':'blur','strength':'medium','expansion':'none'},
                 {'style':'blur','strength':'heavy','expansion':'none'}]
    intervals=[[(1,3),(5,6)],[(3,5),(6,8)]]
    old=render_layered(tmp_path/'old.mkv',source,[first,second],effects.layered_graph(meta,appearances),meta)
    new=render_layered(tmp_path/'new.mkv',source,[first,second],effects.layered_graph(meta,appearances,intervals),meta)
    assert len(new)==len(old)==meta['frame_count']
    for frame in range(meta['frame_count']):
        assert new[frame]==old[frame]
