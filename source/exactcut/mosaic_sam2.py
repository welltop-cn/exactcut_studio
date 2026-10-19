"""Isolated optional SAM 2.1 worker. Run only with locally configured dependencies."""
import json
import sys
import time
from pathlib import Path
from .cutting import run
from . import mosaic_effects as effects
from .util import write_json_atomic, file_sha256


def binary_mask(raw):
    import numpy as np
    if not np.isfinite(raw).all():raise ValueError('SAM 2 returned non-finite mask logits')
    return (raw>0).astype('uint8')*255


def model_overrides(cfg):
    size=cfg.get('inference_size',1024)
    if type(size) is not int or size not in (512,768,1024):raise ValueError('SAM inference_size must be 512, 768 or 1024')
    return [f'++model.image_size={size}'] if size!=1024 else []


def track(request):
    import numpy as np
    import torch
    from PIL import Image, ImageFilter
    from sam2.build_sam import build_sam2_video_predictor
    started=time.monotonic()
    output=Path(request['output']);output.mkdir(parents=True,exist_ok=True)
    source=request['source'];meta=request['metadata'];target=request['target'];cfg=request['config']
    frames=output/'frames';masks=output/'masks';rendered=output/'rendered'
    for path in (frames,masks,rendered):
        path.mkdir(exist_ok=True)
        for old in path.glob('*'):old.unlink()
    count=target['end']-target['start'];rate=f"{meta['fps_num']}/{meta['fps_den']}"
    # Decode and select by source frame index, never infer timestamps from an imprecise seek.
    run(['ffmpeg','-y','-v','error','-i',source,'-an','-vf',f"trim=start_frame={target['start']}:end_frame={target['end']},setpts=PTS-STARTPTS,scale=w='min(640,iw)':h=-2",'-fps_mode','passthrough','-start_number','0','-q:v','2',str(frames/'%06d.jpg')],120)
    files=sorted(frames.glob('*.jpg'))
    if len(files)!=count:raise ValueError('Decoded frame count differs from requested interval')
    with Image.open(files[0]) as frame:width,height=frame.size
    if cfg.get('checkpoint_sha256') and file_sha256(Path(cfg['checkpoint']))!=cfg['checkpoint_sha256']:raise ValueError('Checkpoint hash mismatch')
    device=cfg.get('device','mps')
    torch.set_num_threads(4)
    predictor=build_sam2_video_predictor(cfg.get('model_config','configs/sam2.1/sam2.1_hiera_t.yaml'),cfg['checkpoint'],device=device,apply_postprocessing=False,hydra_overrides_extra=model_overrides(cfg))
    box=np.array([target['x']*width/meta['width'],target['y']*height/meta['height'],(target['x']+target['w'])*width/meta['width'],(target['y']+target['h'])*height/meta['height']],dtype=np.float32)
    seed=target['seed_frame']-target['start'];records={}
    with torch.inference_mode():
        state=predictor.init_state(video_path=str(frames),offload_video_to_cpu=True,offload_state_to_cpu=True)
        predictor.add_new_points_or_box(state,frame_idx=seed,obj_id=1,box=box)
        for reverse in (False,True):
            for index,ids,logits in predictor.propagate_in_video(state,start_frame_idx=seed,reverse=reverse):
                if index in records:continue
                raw=logits[ids.index(1),0].to('cpu').numpy()
                mask=binary_mask(raw)
                if mask.shape!=(height,width):raise ValueError('Unexpected mask dimensions')
                mask=Image.fromarray(mask)
                mask.save(masks/f'{index:06d}.png')
                bounds=mask.getbbox();area=int(np.count_nonzero(np.asarray(mask)))
                records[index]={'frame':target['start']+index,'box':list(bounds) if bounds else None,'area':area}
                if not cfg.get('masks_only'):effects.prepare_mask(mask,cfg.get('effect')).save(rendered/f'{index:06d}.png')
                write_json_atomic(output/'progress.json',{'frames_done':len(records),'frames_total':count})
    ordered=[records[i] for i in range(count)]
    warnings=[]
    for i,item in enumerate(ordered):
        reason='未找到遮罩，已跳过该区域' if not item['area'] else ''
        if i and item['area'] and ordered[i-1]['area'] and max(item['area']/ordered[i-1]['area'],ordered[i-1]['area']/item['area'])>2.5:reason='遮罩面积突变，请检查'
        if reason:warnings.append({'frame':item['frame'],'reason':reason})
    # Batch composition needs raw masks only, not an extra encoded video per target.
    if not cfg.get('masks_only'):
        start=target['start']*meta['fps_den']/meta['fps_num'];duration=count*meta['fps_den']/meta['fps_num']
        run(['ffmpeg','-y','-v','error','-i',source,'-framerate',rate,'-i',str(rendered/'%06d.png'),'-filter_complex',effects.graph(meta,cfg.get('effect'),target['start'],target['end']),'-map','[out]','-an','-r',rate,'-fps_mode','cfr','-c:v','libx264','-pix_fmt','yuv420p','-crf','18','-preset','fast',str(output/'silent.mp4')],120)
        run(['ffmpeg','-y','-v','error','-i',str(output/'silent.mp4'),'-ss',str(start),'-i',source,'-map','0:v:0','-map','1:a:0?','-t',str(duration),'-c:v','copy','-c:a','aac','-movflags','+faststart',str(output/'preview.mp4')],120)
        (output/'silent.mp4').unlink()
    result={'width':width,'height':height,'frames':ordered,'warnings':warnings,'elapsed_seconds':round(time.monotonic()-started,2),'device':device,'inference_size':cfg.get('inference_size',1024),'model':'sam2.1_hiera_tiny','notice':'模型跟踪预览，需人工检查；无警告不代表不存在漏遮。'}
    write_json_atomic(output/'result.json',result)
    for path in (frames,rendered):
        for file in path.glob('*'):file.unlink()
        path.rmdir()
    return result


if __name__=='__main__':
    track(json.loads(Path(sys.argv[1]).read_text()))
