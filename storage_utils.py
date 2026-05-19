import os
import uuid

from werkzeug.utils import secure_filename


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
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


def _client():
    if not storage_enabled():
        raise StorageError("Supabase Storage is not configured.")

    from supabase import create_client

    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _response_data(response):
    if isinstance(response, dict):
        return response.get("data") or response

    data = getattr(response, "data", None)
    if data is not None:
        return data

    return response


def make_storage_path(filename):
    safe_name = secure_filename(filename or "")
    lower_name = safe_name.lower()
    if not lower_name.endswith(ALLOWED_EXTENSIONS):
        raise StorageError("Please upload a PPTX or PDF file.")

    return f"learner_uploads/{uuid.uuid4().hex}_{safe_name}"


def create_signed_upload(path):
    response = (
        _client()
        .storage
        .from_(SUPABASE_STORAGE_BUCKET)
        .create_signed_upload_url(path)
    )
    data = _response_data(response) or {}
    token = data.get("token") or data.get("Token")
    signed_url = data.get("signedURL") or data.get("signedUrl") or data.get("signed_url")

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

    response = (
        _client()
        .storage
        .from_(SUPABASE_STORAGE_BUCKET)
        .download(path)
    )
    return _response_data(response)


def remove_storage_file(path):
    if not path or ".." in path or path.startswith("/"):
        return

    try:
        _client().storage.from_(SUPABASE_STORAGE_BUCKET).remove([path])
    except Exception:
        pass
