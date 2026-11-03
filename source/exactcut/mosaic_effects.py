"""Shared, versioned mosaic appearance contract for previews and full renders."""
from PIL import ImageFilter, ImageChops
from . import cutting

DEFAULT={'style':'pixel','strength':'medium','expansion':'small'}
CHOICES={'style':('pixel','blur','soft_blur','solid'),'strength':('light','medium','heavy'),'expansion':('none','small','medium','large')}


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
    if effect['style']!='soft_blur' or not core.getbbox():return core
    # Feather outward only: preserve full coverage of the selected core.
    radius=max(1,round(min(core.size)*.015))
    outer=core.filter(ImageFilter.MaxFilter(radius*2+1)).filter(ImageFilter.GaussianBlur(radius))
    return ImageChops.lighter(core,outer)


def transform(meta,effect=None):
    effect=validate(effect);w,h=meta['width'],meta['height']
    strength=effect['strength']
    # Keep appearance proportional to the 640px preview baseline.
    ratio=max(w,h)/640
    block=max(1,round({'light':16,'medium':30,'heavy':50}[strength]*ratio))
    if effect['style']=='pixel':transform=f'scale={max(1,w//block)}:{max(1,h//block)}:flags=area,scale={w}:{h}:flags=neighbor'
    elif effect['style'] in ('blur','soft_blur'):transform=f"gblur=sigma={ {'light':8,'medium':16,'heavy':28}[strength]*ratio:g}:steps=2"
    else:transform='lutrgb=r=0:g=0:b=0'
    return transform


def graph(meta,effect=None,start=None,end=None):
    w,h=meta['width'],meta['height']
    appearance=transform(meta,effect)
    trim=f'trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,' if start is not None else ''
    return (f'[0:v]{trim}format=gbrp,split=2[original][small];'
            f'[small]{appearance}[pixel];'
            f'[1:v]settb=expr={meta["fps_den"]}/{meta["fps_num"]},setpts=N,scale={w}:{h}:flags=neighbor,format=gbrp[mask];'
            '[original][pixel][mask]maskedmerge,format=yuv420p[out]')


def layered_graph(meta,appearances,intervals=None):
    # Each effect samples the original image; later layers win in overlapping regions.
    # intervals optionally carries the source-frame spans with a mask per layer;
    # blur only runs there so untouched frames cost nothing.
    count=len(appearances)
    if intervals is None:intervals=[None]*count
    result='[0:v]format=gbrp,split='+str(count+1)+'[base]'+''.join(f'[source{i}]' for i in range(count))+';'
    previous='base'
    for i,(effect,spans) in enumerate(zip(appearances,intervals)):
        operation=transform(meta,effect)
        if spans is not None and effect['style'] in ('blur','soft_blur'):
            if spans:
                enable='+'.join(f'gte(n,{start})*lt(n,{end})' for start,end in spans)
                operation+=f":enable='{enable}'"
            else:operation='null'
        mask=f'settb=expr={meta["fps_den"]}/{meta["fps_num"]},setpts=N,scale={meta["width"]}:{meta["height"]}:flags=neighbor,format=gbrp'
        result+=f'[source{i}]{operation}[effect{i}];[{i+1}:v]{mask}[mask{i}];'
        result+=f'[{previous}][effect{i}][mask{i}]maskedmerge[layer{i}];'
        previous=f'layer{i}'
    return result+f'[{previous}]format=yuv420p[out]'
