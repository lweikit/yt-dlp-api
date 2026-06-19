"""Shared Olevod helpers used by both the FastAPI app and the yt-dlp extractor plugin.

Kept as plain module-level functions (no yt-dlp imports beyond AES) so it can be
imported from app.py at /app and from the plugin copied into
/app/yt_dlp_plugins/extractor/ (both resolve via the working directory on sys.path).
"""
import base64
import hashlib
import json
import time

from yt_dlp.aes import aes_cbc_decrypt_bytes, unpad_pkcs7


def make_vv(ts=None):
    """Build the `_vv` signature query param the Olevod API expects."""
    ts = str(int(ts) if ts else int(time.time()))
    bits = ['', '', '', '']
    for char in ts:
        encoded = format(ord(char), 'b')
        bits[0] += encoded[2:3]
        bits[1] += encoded[3:4]
        bits[2] += encoded[4:5]
        bits[3] += encoded[5:]
    inserts = [format(int(part, 2), 'x').zfill(3) if part else '000' for part in bits]
    digest = hashlib.md5(ts.encode()).hexdigest()
    return ''.join((
        digest[:3], inserts[0],
        digest[6:11], inserts[1],
        digest[14:19], inserts[2],
        digest[22:27], inserts[3],
        digest[30:],
    ))


def decrypt_api_data(data):
    """Decrypt an Olevod API `data` blob. Returns parsed JSON, or None on failure."""
    if not isinstance(data, str):
        return data
    now = int(time.time())
    for offset in (0, 86400, -86400):
        date_str = time.strftime('%Y-%m-%d', time.localtime(now + offset))
        key = hashlib.md5(date_str.encode()).hexdigest()[8:24].encode()
        try:
            decrypted = unpad_pkcs7(aes_cbc_decrypt_bytes(base64.b64decode(data), key, key)).decode()
            return json.loads(decrypted)
        except Exception:
            continue
    return None
