"""Create an explicitly synthetic completed asset in the isolated dev workbench."""
from pathlib import Path
from exactcut import workflow as w, accounts
from exactcut.media import run_checked
from exactcut.runtime import environment, data_root, CODE_ROOT
from exactcut.util import read_json, write_json_atomic, file_sha256


def main():
    root = data_root()
    if environment() != 'development' or not root.is_relative_to(CODE_ROOT/'.dev-state'):
        raise RuntimeError('此工具仅用于本仓库 .dev-state 隔离开发数据')
    marker = root/'.web-state/cutting-fixture.json'
    if marker.exists() and (w.BATCHES/read_json(marker)['id']/'draft.json').exists():
        print(read_json(marker)['id']); return
    item = w.create_blank(display_name='剪切验收 · 本机测试视频')
    draft = item['draft']
    draft.update(drama_id='999991', drama_title='剪切验收 · 本机测试视频', author='本机演示', business_date='20261012')
    row = draft['rows'][0]
    row.update(title_cn='逐帧剪切演示（合成测试画面）', hook_text='')
    row['hook_options'].update(mode='none', position='top')
    row['clips'][0].update(episode='1', end_s='18')
    saved = w.save_draft(item['id'], {'revision': item['revision'], 'draft': draft})
    w.start_batch(item['id'], saved['revision'])
    batch = w.folder(item['id'])
    accounts.assign(batch, {'username': 'admin'})
    _, request = w.request_for(batch, row['request_id'])
    master = batch/'production/videos'/request.output_filename
    master.parent.mkdir(parents=True, exist_ok=True)
    run_checked(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=360x640:rate=30',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '18',
                 '-vf', "drawtext=fontfile=/System/Library/Fonts/Monaco.ttf:text='FRAME %{n}':x=20:y=30:fontsize=28:fontcolor=white:box=1:boxcolor=black",
                 '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                 '-movflags', '+faststart', str(master)])
    write_json_atomic(batch/'production/results.json', {'requests': {request.request_id: {
        'status': 'qc_passed', 'output_sha256': file_sha256(master), 'duration_ms': 18000}}})
    w.update(batch, 'review', '本机合成测试成片已就绪，可进入成片预览体验剪切')
    write_json_atomic(marker, {'id': item['id']})
    print(item['id'])


if __name__ == '__main__':
    main()
