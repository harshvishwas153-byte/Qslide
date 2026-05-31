import os
import json
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, parse_qs
from urllib.request import Request, urlopen

from werkzeug.utils import secure_filename


def _normalize_supabase_url(value):
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url


SUPABASE_URL = _normalize_supabase_url(os.environ.get("SUPABASE_URL", ""))
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "uploads").strip()

ALLOWED_EXTENSIONS = (".ppt", ".pptx", ".pdf")


class StorageError(ValueError):
    pass


def storage_enabled():
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY and SUPABASE_STORAGE_BUCKET)


def public_storage_config():
    return {
        "enabled": storage_enabled(),
        "url": SUPABASE_URL if storage_enabled() else "",
        "anon_key": SUPABASE_ANON_KEY if storage_enabled() else "",
        "bucket": SUPABASE_STORAGE_BUCKET if storage_enabled() else "",
    }


def _storage_headers(content_type="application/json"):
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _storage_url(path):
    return f"{SUPABASE_URL}/storage/v1{path}"


def _read_error_body(error):
    try:
        body = error.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return data.get("message") or data.get("error") or body
    except json.JSONDecodeError:
        pass
    return body


def _storage_request(method, path, body=None, content_type="application/json"):
    if not storage_enabled():
        raise StorageError("Supabase Storage is not configured.")

    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")

    request = Request(
        _storage_url(path),
        data=payload,
        headers=_storage_headers(content_type),
        method=method,
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read()
            content_type_header = response.headers.get("Content-Type", "")
            if "application/json" in content_type_header:
                return json.loads(raw.decode("utf-8") or "{}")
            return raw
    except HTTPError as e:
        detail = _read_error_body(e)
        raise StorageError(f"Supabase Storage returned {e.code}: {detail or e.reason}") from e
    except URLError as e:
        raise StorageError(f"Could not reach Supabase Storage: {e.reason}") from e


def make_storage_path(filename):
    safe_name = secure_filename(filename or "")
    lower_name = safe_name.lower()
    if not lower_name.endswith(ALLOWED_EXTENSIONS):
        raise StorageError("Please upload a PPTX or PDF file.")

    return f"learner_uploads/{uuid.uuid4().hex}_{safe_name}"


def create_signed_upload(path):
    bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
    object_path = quote(path, safe="/")
    data = _storage_request(
        "POST",
        f"/object/upload/sign/{bucket}/{object_path}",
        {},
    )
    signed_url = (
        data.get("signedURL")
        or data.get("signedUrl")
        or data.get("signed_url")
        or data.get("url")
    )
    if signed_url and signed_url.startswith("/"):
        signed_url = _storage_url(signed_url)

    token = data.get("token") or data.get("Token")
    if not token and signed_url:
        token = (parse_qs(urlparse(signed_url).query).get("token") or [""])[0]

    if not token:
        raise StorageError("Could not create a Supabase upload token.")

    return {
        "path": data.get("path") or path,
        "token": token,
        "signed_url": signed_url,
    }


def download_storage_file(path):
    if not path or ".." in path or path.startswith("/"):
        raise StorageError("Invalid storage file path.")

    bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
    object_path = quote(path, safe="/")
    return _storage_request("GET", f"/object/{bucket}/{object_path}", content_type=None)


def remove_storage_file(path):
    if not path or ".." in path or path.startswith("/"):
        return

    try:
        bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
        _storage_request("DELETE", f"/object/{bucket}", {"prefixes": [path]})
    except Exception:
        pass
