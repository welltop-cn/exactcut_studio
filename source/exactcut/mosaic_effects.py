"""Shared, versioned mosaic appearance contract for previews and full renders."""
from PIL import ImageFilter, ImageChops
from . import cutting

DEFAULT={'style':'pixel','strength':'medium','expansion':'small'}
CHOICES={'style':('pixel','blur','solid'),'strength':('light','medium','heavy'),'expansion':('none','small','medium','large')}


def validate(value=None):
    if value is None:return dict(DEFAULT)
    if not isinstance(value,dict) or set(value)-set(DEFAULT):raise cutting.CutError('打码效果设置无效')
    result={**DEFAULT,**value}
    if any(result[k] not in choices for k,choices in CHOICES.items()):raise cutting.CutError('打码效果选项无效')
    return result


def expand(mask,level):
    radius=round(min(mask.size)*{'none':0,'small':.008,'medium':.016,'large':.03}[level])
    return mask.filter(ImageFilter.MaxFilter(radius*2+1)) if radius else mask


def prepare_mask(mask,effect=None):
    effect=validate(effect)
    core=expand(mask,effect['expansion'])
    if effect['style']!='blur' or not core.getbbox():return core
    # Feather outward only: preserve full coverage of the selected core.
    radius=max(1,round(min(core.size)*.015))
    outer=core.filter(ImageFilter.MaxFilter(radius*2+1)).filter(ImageFilter.GaussianBlur(radius))
    return ImageChops.lighter(core,outer)


def graph(meta,effect=None,start=None,end=None):
    effect=validate(effect);w,h=meta['width'],meta['height']
    strength=effect['strength']
    block={'light':16,'medium':30,'heavy':50}[strength]
    if effect['style']=='pixel':transform=f'scale={max(1,w//block)}:{max(1,h//block)}:flags=area,scale={w}:{h}:flags=neighbor'
    elif effect['style']=='blur':transform=f"gblur=sigma={ {'light':8,'medium':16,'heavy':28}[strength]}:steps=2"
    else:transform='lutrgb=r=0:g=0:b=0'
    trim=f'trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,' if start is not None else ''
    return (f'[0:v]{trim}format=gbrp,split=2[original][small];'
            f'[small]{transform}[pixel];'
            f'[1:v]settb=expr={meta["fps_den"]}/{meta["fps_num"]},setpts=N,scale={w}:{h}:flags=neighbor,format=gbrp[mask];'
            '[original][pixel][mask]maskedmerge,format=yuv420p[out]')
