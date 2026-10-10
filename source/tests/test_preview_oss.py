from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import pytest
from exactcut import preview_oss


class Bucket:
    bucket_name = "fixture"
    endpoint = "https://oss.invalid"
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.signs = []

    def object_exists(self, key):
        return key in self.objects

    def put_object_from_file(self, key, filename, headers):
        from pathlib import Path
        assert key.startswith('exactcut-previews/production/')
        assert headers['x-oss-forbid-overwrite'] == 'true'
        assert key not in self.objects
        self.puts.append(key)
        self.objects[key] = (Path(filename).read_bytes(), headers)
        return SimpleNamespace(status=200)

    def head_object(self, key):
        content, headers = self.objects[key]
        return SimpleNamespace(content_length=len(content), headers=headers)

    def get_object_acl(self, key):
        return SimpleNamespace(acl=self.objects[key][1]['x-oss-object-acl'])

    def sign_url(self, method, key, expires):
        self.signs.append((method, key, expires))
        return 'https://fixture.invalid/' + key + '?signature=fixture'


def test_private_content_addressed_upload_is_reused_without_overwrite(tmp_path):
    path = tmp_path / 'preview.mp4'; path.write_bytes(b'video')
    bucket = Bucket()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: preview_oss.signed_preview(path, 'a'*32, '素材/1', bucket=bucket), range(2)))
    assert len(bucket.puts) == 1
    key = bucket.puts[0]
    assert key.endswith('/'+hashlib.sha256(b'video').hexdigest()+'.mp4')
    assert bucket.objects[key][1]['x-oss-object-acl'] == 'private'
    assert all(r['url'].startswith('https://fixture.invalid/') and r['expires_in'] == 900 for r in results)
    path.write_bytes(b'changed video')
    preview_oss.signed_preview(path, 'a'*32, '素材/1', bucket=bucket)
    assert len(bucket.puts) == 2
    assert bucket.objects[key][0] == b'video'


@pytest.mark.parametrize('damage', ['acl', 'hash', 'size'])
def test_existing_object_mismatch_is_rejected_without_modification(tmp_path, damage):
    path = tmp_path / 'preview.mp4'; path.write_bytes(b'video')
    bucket = Bucket()
    preview_oss.signed_preview(path, 'a'*32, 'R1', bucket=bucket)
    key = bucket.puts[0]; content, headers = bucket.objects[key]
    if damage == 'acl': headers['x-oss-object-acl'] = 'public-read'
    if damage == 'hash': headers['x-oss-meta-sha256'] = 'wrong'
    if damage == 'size': bucket.objects[key] = (b'wrong size', headers)
    with pytest.raises(ValueError):
        preview_oss.signed_preview(path, 'a'*32, 'R1', bucket=bucket)
    assert len(bucket.puts) == 1
    assert len(bucket.signs) == 1


def test_nonproduction_never_connects_to_oss(monkeypatch):
    monkeypatch.setenv('EXACTCUT_ENVIRONMENT', 'test')
    assert not preview_oss.enabled()


def test_generated_preview_upload_receipt_allows_sign_only(tmp_path, monkeypatch):
    batch = tmp_path / ('a'*32) / 'production'
    path = batch / 'previews/video.mp4'; path.parent.mkdir(parents=True);path.write_bytes(b'video')
    record = {'status':'qc_passed', 'output_sha256':'master', 'preview':{'master_sha256':'master'}}
    bucket = Bucket()
    monkeypatch.setattr(preview_oss, 'enabled', lambda: True)
    monkeypatch.setattr(preview_oss, '_connect', lambda: bucket)
    request = SimpleNamespace(request_id='R1', output_filename='video.mp4')
    preview_oss.upload_generated_preview(batch, request, record)
    assert len(bucket.puts) == 1
    assert record['preview']['oss']['key'] == bucket.puts[0]
    bucket.object_exists = lambda key: pytest.fail('ready preview must only need signing')
    result = preview_oss.signed_preview(path, 'a'*32, 'R1', bucket=bucket, receipt=record['preview']['oss'])
    assert result['url'].startswith('https://fixture.invalid/')
    assert len(bucket.puts) == 1


def test_generated_preview_upload_failure_preserves_local_master_and_records_retry(tmp_path, monkeypatch):
    batch = tmp_path / ('a'*32) / 'production'
    path = batch / 'previews/video.mp4'; path.parent.mkdir(parents=True);path.write_bytes(b'video')
    record = {'status':'qc_passed','output_sha256':'master','preview':{'master_sha256':'master'}}
    monkeypatch.setattr(preview_oss,'enabled',lambda:True)
    def fail(): raise RuntimeError('secret upstream')
    monkeypatch.setattr(preview_oss,'_connect',fail)
    preview_oss.upload_generated_preview(batch, SimpleNamespace(request_id='R1',output_filename='video.mp4'), record)
    assert record['status'] == 'qc_passed'
    assert path.read_bytes() == b'video'
    assert record['preview_oss_error']
    assert 'secret' not in record['preview_oss_error']


def test_receipt_hot_path_does_not_hash_or_query_oss(tmp_path, monkeypatch):
    video=tmp_path/'preview.mp4';video.write_bytes(b'video');bucket=Bucket()
    receipt=preview_oss._ensure_uploaded(video,'a'*32,'R1',bucket=bucket)
    monkeypatch.setattr(preview_oss,'file_sha256',lambda *a:pytest.fail('hot path should use verified file identity'))
    bucket.object_exists=lambda *a:pytest.fail('hot path must not query OSS')
    assert preview_oss.signed_preview(video,'a'*32,'R1',bucket=bucket,receipt=receipt)['expires_in']==900


def test_upload_reads_snapshot_not_replaced_source(tmp_path, monkeypatch):
    video=tmp_path/'preview.mp4';video.write_bytes(b'first');bucket=Bucket()
    # Reproduce filesystems whose timestamp resolution hides rapid same-size writes.
    identity = preview_oss._identity(video)
    monkeypatch.setattr(preview_oss, '_identity', lambda _path: identity)
    put=bucket.put_object_from_file
    def replace(key,filename,headers):
        assert filename != str(video)
        video.write_bytes(b'other')
        return put(key,filename,headers)
    bucket.put_object_from_file=replace
    with pytest.raises(ValueError,match='更新'):
        preview_oss.signed_preview(video,'a'*32,'R1',bucket=bucket)
    assert next(iter(bucket.objects.values()))[0] == b'first'


def test_original_download_uses_private_master_and_signed_attachment(tmp_path):
    video=tmp_path/'正式成片.mp4';video.write_bytes(b'original master');bucket=Bucket();seen={}
    def sign(method,key,expires,params=None):
        seen.update(key=key,expires=expires,params=params)
        return 'https://fixture.invalid/download'
    bucket.sign_url=sign
    result=preview_oss.signed_preview(video,'a'*32,'R1',bucket=bucket,download_name=video.name)
    assert result['url']=='https://fixture.invalid/download'
    assert next(iter(bucket.objects.values()))[0]==b'original master'
    assert seen['params']['response-content-disposition'].startswith('attachment;')
    assert "filename*=UTF-8''" in seen['params']['response-content-disposition']


def test_fbbm_reuses_private_master_by_server_side_copy(tmp_path,monkeypatch):
    from exactcut.publish import OssPublisher,guarded_delivery
    from unittest.mock import Mock
    video=tmp_path/'20260902_typezxraw_张三_AGENT_12_#101460#.mp4';video.write_bytes(b'master')
    digest=hashlib.sha256(b'master').hexdigest()
    publisher=OssPublisher();publisher.bucket_name='fixture';publisher.endpoint='https://oss.invalid'
    bucket=Mock();bucket.object_exists.return_value=False
    bucket.head_object.return_value=SimpleNamespace(content_length=6,headers={'x-oss-meta-sha256':digest,'ETag':'"etag"'})
    bucket.copy_object.return_value.status=200
    bucket.get_object_meta.return_value=SimpleNamespace(headers={'x-oss-meta-sha256':digest})
    monkeypatch.setattr(publisher,'connect',lambda:bucket)
    receipt={'bucket':'fixture','endpoint':'https://oss.invalid','key':f'exactcut-previews/production/{"a"*32}/R1/{digest}.mp4','sha256':digest,'bytes':6}
    with guarded_delivery(oss_source=receipt):
        result=publisher.upload(video)
    assert result['sha256']==digest
    bucket.put_object_from_file.assert_not_called()
    args,kw=bucket.copy_object.call_args
    assert args[0]=='fixture' and args[1]==receipt['key']
    assert args[2].startswith('fb-bm-assets/')
    assert kw['headers']['x-oss-forbid-overwrite']=='true'
    assert kw['headers']['x-oss-copy-source-if-match']=='"etag"'
