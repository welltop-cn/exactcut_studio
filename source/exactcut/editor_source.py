"""Read-only playback resolution for a SHA-bound editing session.

Contract: source (oss/local), url, expires_in (seconds), reason (finite enum).
The caller owns authentication, project access and the batch lease. This module
never uploads, hashes media, probes OSS, or persists signed bearer URLs.
"""
import re
from urllib.parse import quote

from . import cutting, cutting_access, preview_oss, workflow


def enabled():
    return cutting.settings().get('oss_playback_enabled', False) is True


def resolve(batch_id, session, local_url):
    rid, sha = session['request_id'], session['source_sha256']
    batch = workflow.folder(batch_id)
    def check():
        cutting_access.require_editable(batch, rid, source_sha=sha)
    def local(reason):
        return {'source': 'local', 'url': local_url, 'expires_in': 0, 'reason': reason}
    check()
    result = local('disabled')
    if enabled() and preview_oss.enabled():
        # Business/version failures must remain failures, outside OSS fallback.
        master, record = workflow.preview_file(batch_id, rid, lightweight=False)
        if record.get('output_sha256') != sha:
            raise cutting.CutError('当前成片已变化，请刷新后重新编辑', 409)
        receipt = record.get('original_oss')
        result = local('receipt_unavailable')
        if isinstance(receipt, dict):
            try:
                bucket = preview_oss._connect()
                identity = preview_oss._identity(master)
                key = f'{preview_oss.object_prefix()}/{batch_id}/{quote(rid, safe="")}/{sha}.mp4'
                if (re.fullmatch(r'[0-9a-f]{64}', sha) and receipt.get('sha256') == sha and
                        receipt.get('key') == key and receipt.get('identity') == identity and
                        receipt.get('bytes') == identity[2] and receipt.get('bucket') == bucket.bucket_name and
                        receipt.get('endpoint') == bucket.endpoint):
                    url = bucket.sign_url('GET', key, 900)
                    result = {'source': 'oss', 'url': url, 'expires_in': 900, 'reason': 'verified'}
            except Exception:
                # Credentials/SDK exceptions can contain secrets. Return only an enum.
                result = local('unavailable')
    check()
    return result
