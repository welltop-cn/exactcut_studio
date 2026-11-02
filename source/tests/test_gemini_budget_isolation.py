"""Gemini bypasses local rate budgets; OpenAI responses remain budgeted."""
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from exactcut import ai_rate,cutting_scan,mosaic_locate,preview_subtitle_model,safety,hook_emphasis,preview_subtitles
from exactcut.stage_results import ResourceDeferred
from test_cutting_api import ready
from test_phase2_preview_api import fixture


@pytest.mark.parametrize('operation',['scan','locate','subtitle','safety-images','safety-video'])
def test_gemini_ignores_exhausted_openai_bucket_and_never_changes_it(ready,tmp_path,monkeypatch,operation):
    from google import genai
    bid,rid,sha,source=ready
    monkeypatch.setattr(ai_rate,'data_root',lambda:tmp_path/'budget')
    monkeypatch.setattr(ai_rate,'load_config',lambda:{'concurrency':{'ai':{'requests_per_minute':6,'burst':1}}})
    monkeypatch.setattr(ai_rate.time,'time',lambda:100.)
    ai_rate.acquire();bucket=tmp_path/'budget/.web-state/ai-rate.json';before=bucket.read_bytes()
    def unexpected_wait(*a):raise AssertionError('Gemini waited for a local request permit')
    monkeypatch.setattr(time,'sleep',unexpected_wait)
    response={'scan':'{"risk_events":[]}','locate':'{"candidates":[]}','subtitle':'{"cues":[]}','safety-images':'{"decision":"pass"}','safety-video':'{"risk_events":[]}'}[operation]
    client=Mock();client.models.generate_content.return_value=SimpleNamespace(text=response)
    client.files.upload.return_value.name='files/fixture';client.files.get.return_value=SimpleNamespace(state='ACTIVE',uri='fixture-uri')
    monkeypatch.setattr(genai,'Client',lambda **kwargs:client)
    if operation=='scan':
        cfg={**cutting_scan.settings(),'max_refine_events':0};cutting_scan.detect(source,{'frame_count':90,'fps_num':30,'fps_den':1},cfg,'standard','fixture',lambda *a:None,SimpleNamespace(close=lambda:None))
    elif operation=='locate':mosaic_locate.detect(b'jpeg',{'width':160,'height':96},cutting_scan.settings(),'fixture')
    elif operation=='subtitle':preview_subtitle_model.ask(client,source,3000,preview_subtitles.settings(),time.monotonic()+500)
    elif operation=='safety-images':safety._gemini_images_json(client,'fixture','prompt',[])
    else:
        monkeypatch.setattr(safety,'_gemini_client',lambda:client)
        assert safety.final_video_gemini(source,{'safety':{'final_video_model':'fixture'}},tmp_path/'gemini.json')['status']=='passed'
    client.models.generate_content.assert_called_once()
    assert bucket.read_bytes()==before
    # A real OpenAI entrance still refuses before opening HTTP when tokens are gone.
    monkeypatch.setattr(hook_emphasis,'require_ai',lambda:None);monkeypatch.setattr(hook_emphasis,'load_env_file',lambda *a:None);monkeypatch.setenv('OPENAI_API_KEY','fixture')
    http=Mock(side_effect=AssertionError('OpenAI must obey its own local budget'));monkeypatch.setattr(hook_emphasis.httpx,'Client',http)
    with pytest.raises(ResourceDeferred):hook_emphasis.responses_json('prompt','text',{},'fixture','fixture')
    http.assert_not_called()
