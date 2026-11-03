"""HTTP parser concurrency uses an isolated provider transport, never real paid AI."""
import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from exactcut import ai_rate,llm_intake,workflow
from test_phase2_preview_api import fixture,serve,call
from test_clip_text import response,item
from test_llm_intake import interpreted,TEMPLATE


def test_text_and_excel_http_can_enter_provider_together_with_exhausted_bucket(fixture,tmp_path,monkeypatch):
    monkeypatch.setattr(ai_rate,'data_root',lambda:tmp_path)
    monkeypatch.setattr(ai_rate,'load_config',lambda:{'concurrency':{'ai':{'requests_per_minute':6,'burst':1}}})
    monkeypatch.setattr(ai_rate.time,'time',lambda:100.)
    ai_rate.acquire();bucket=tmp_path/'.web-state/ai-rate.json';before=bucket.read_bytes()
    monkeypatch.setattr(llm_intake,'require_ai',lambda:None);monkeypatch.setattr(llm_intake,'load_env_file',lambda *a:None)
    monkeypatch.setattr(workflow,'read_excel',llm_intake.read_excel)
    monkeypatch.setenv('OPENAI_API_KEY','fixture');monkeypatch.setenv('OPENAI_BASE_URL','https://fixture.invalid/v1')
    condition=threading.Condition();seen=[];release=threading.Event()
    real_client=httpx.Client
    def transport(request):
        name=json.loads(request.content)['text']['format']['name']
        with condition:seen.append(name);condition.notify_all()
        assert release.wait(5)
        payload=interpreted().model_dump() if name=='excel_interpretation' else response([item([1])])
        return httpx.Response(200,json={'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(payload)}]}]})
    monkeypatch.setattr(llm_intake.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(transport),**kw))
    app=serve()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures=[pool.submit(call,app,'POST','/api/clip-text',{'text':f'30 1:20-1:47\n' if i==0 else '30 1:20-1:47'}) for i in range(2)]
            futures.extend(pool.submit(call,app,'POST','/api/batches/import',{'filename':f'parallel-{i}.xlsx','content':base64.b64encode(TEMPLATE.read_bytes()).decode()}) for i in range(2))
            try:
                with condition:assert condition.wait_for(lambda:len(seen)==4,2),seen
            finally:release.set()
            results=[future.result() for future in futures]
        assert [result[0] for result in results]==[200,200,201,201],results
        assert bucket.read_bytes()==before
        assert results[2][1]['id']!=results[3][1]['id']
    finally:release.set();app.shutdown();app.server_close()
