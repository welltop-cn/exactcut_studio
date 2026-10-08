from exactcut.intake import timing_explanation
from exactcut.models import BatchManifest


def manifest(timeline):
    return BatchManifest(schema_version=1, drama_id='101460', drama_title='', business_date='20261007', author='测试', source_workbook='',
        requests=[dict(request_id='R1', seq=1, output_filename='test.mp4', timeline=timeline)])


def test_twenty_second_plan_explains_original_endpoints_and_card_duration():
    item = manifest([dict(type='episode', episode='EP1', start_ms=2000, end_ms=22000),
                     dict(type='title_card', text='One Hour Earlier', duration_ms=1500)])
    explanation = timing_explanation(item)[0]
    assert explanation['segments'][0] == {'segment':1,'type':'episode','episode':'EP1','start_ms':2000,
                                         'end_ms':22000,'text':None,'duration_ms':20000}
    assert explanation['planned_duration_ms'] == 21500
    assert explanation['pending_endings'] == 0


def test_episode_endings_are_not_fabricated_before_source_video_available():
    item = manifest([dict(type='episode',episode='EP1',start_ms=0,full_episode=True),
                     dict(type='episode',episode='EP2',start_ms=12000,to_end=True),
                     dict(type='episode',episode='EP3',start_ms=1000,end_ms=21000)])
    explanation=timing_explanation(item)[0]
    assert explanation['planned_duration_ms'] is None
    assert explanation['pending_endings']==2 and explanation['known_duration_ms']==20000
    assert [s['duration_ms'] for s in explanation['segments']]==[None,None,20000]
