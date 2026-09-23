"""backend/photo_storage.py — where student photo files live, and the ONE way to read one back.

Every screen that shows a student photo goes through `load_photo` / `photo_response` below: the
operator card and Admin → Students (`/photo/{id}`), the Stage slots (same route), the public LED
(`/led/photo/{key}`) and the printed pass (`passes._prepare_photo`). There is no second
path-building rule anywhere else.

THE DATABASE KEY IS THE SAME EVERYWHERE
  `students.photo_path` holds `photos/<file name>` — exactly the value the 1,199 existing rows
  already hold. The key says WHICH photo, never WHERE its bytes are; the configured store decides
  that. This is deliberate: after "Freeze display data" a student's photo_path can only change
  through a logged master patch (migration 0010), so a design that had to rewrite every key to
  move the photos somewhere else could not move the frozen students' photos at all.

TWO STORES, ONE INTERFACE (`PHOTO_STORAGE`)
  * `local` (the default): files in a folder — `photos/` at the project root, `/app/photos` in
    the container (docker-compose bind-mounts it), or `PHOTO_STORAGE_DIR`. Local development
    needs no Cloudinary account and no internet.
  * `cloudinary` (production on Render, whose own disk is wiped on every deploy): the bytes are
    uploaded to Cloudinary as PRIVATE ("authenticated") images under one project folder. The
    public ID is a hash of the key, so no student name or PRN appears in Cloudinary and the same
    photo always lands on the same public ID (a re-run never makes a copy). The browser never
    sees a Cloudinary URL: the server fetches the image with a signed URL and serves it from the
    app's own routes, so the existing sign-in rule on `/photo` and the opaque LED key still hold,
    and no credential or asset ID reaches a page.

A MISCONFIGURED STORE FAILS LOUDLY
  `PHOTO_STORAGE=cloudinary` with a credential missing, an unknown `PHOTO_STORAGE`, or a
  `PHOTO_STORAGE_DIR` that does not exist stops the server at start-up with one plain sentence
  naming the setting (never its value). A folder is never created for an explicitly configured
  `PHOTO_STORAGE_DIR`: a volume that is not mounted would otherwise "work" and lose every photo on
  the next restart — the earlier missing-volume bug in this codebase. On Render (`RENDER` set) with
  local storage the server still starts, so scanning keeps working, but every photo WRITE is
  refused with a plain message instead of going to a disk that will be wiped.
"""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import io
import logging
import mimetypes
import os
import pathlib
import secrets
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Protocol, Union

logger = logging.getLogger("backend.photos")

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLACEHOLDER = PROJECT_ROOT / "static" / "placeholder.svg"
KEY_PREFIX = "photos"
DEFAULT_LOCAL_ROOT = PROJECT_ROOT / KEY_PREFIX
DEFAULT_CLOUDINARY_FOLDER = "convocation/student-photos"

RENDER_LOCAL_REFUSAL = ("This server has no permanent photo storage, so photos cannot be saved here. "
                        "Set up Cloudinary photo storage first.")


class PhotoStoreError(Exception):
    """One photo could not be stored. `str(exc)` is a plain sentence, safe to show an Admin."""


class PhotoStorageConfigError(Exception):
    """Photo storage is misconfigured. The message names the setting, never a credential."""


@dataclasses.dataclass(frozen=True)
class PhotoBlob:
    """A photo found by the resolver: a local file (`path`) or bytes fetched from Cloudinary (`data`)."""
    media_type: str
    path: Optional[pathlib.Path] = None
    data: Optional[bytes] = None

    def open(self) -> Union[pathlib.Path, io.BytesIO]:
        return self.path if self.path is not None else io.BytesIO(self.data or b"")


class PhotoStore(Protocol):
    kind: str
    write_refusal: Optional[str]

    def key_for(self, filename: str) -> str: ...
    def save(self, filename: str, data: bytes) -> str: ...
    def load(self, key: Optional[str]) -> Optional[PhotoBlob]: ...
    def describe(self) -> str: ...


# ─────────────────────────────────────────────────────────────────────── keys
def normalise_key(raw: Optional[str]) -> Optional[str]:
    """`photos/<name>` for any spelling of a stored photo reference, or None.

    `photos/a.jpg`, `photos\\a.jpg` (a Windows import) and `E:\\...\\1QR\\photos\\a.jpg` (an absolute
    Windows path) all give `photos/a.jpg` — the same rule as scripts/fix_photo_paths.py. A value with
    no `photos` segment keeps only its file name. `..` is never allowed through.
    """
    if raw is None or not str(raw).strip():
        return None
    parts = [p for p in str(raw).strip().replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    lowered = [p.lower() for p in parts]
    rest = parts[lowered.index(KEY_PREFIX) + 1:] if KEY_PREFIX in lowered else parts[-1:]
    if not rest or ".." in rest or ":" in rest[0]:
        return None
    return KEY_PREFIX + "/" + "/".join(rest)


def _check_filename(filename: str) -> None:
    if not filename or filename in (".", "..") or any(c in filename for c in "/\\\0"):
        raise PhotoStoreError("The photo has an unusable file name.")


def _media_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


# ─────────────────────────────────────────────────────────────────────── local
class LocalPhotoStore:
    """Photos as files in one folder. The key prefix is `photos` except for the legacy
    `import_photos_from_zip(dest_dir=...)` call, which keeps writing `<dest_dir>/<name>` keys."""

    kind = "local"

    def __init__(self, root: Union[str, pathlib.Path], *, key_prefix: str = KEY_PREFIX,
                 write_refusal: Optional[str] = None):
        self.root = pathlib.Path(root)
        self.key_prefix = key_prefix.rstrip("/") or KEY_PREFIX
        self.write_refusal = write_refusal

    def describe(self) -> str:
        return f"local folder {self.root.as_posix()}"

    def key_for(self, filename: str) -> str:
        return f"{self.key_prefix}/{filename}"

    def save(self, filename: str, data: bytes) -> str:
        """Write one photo. An existing non-empty file of the same name is kept, never replaced.
        Written to a temporary name first and renamed into place, so a half-written file can never
        sit under the real name and be mistaken for a photo on the next run."""
        if self.write_refusal:
            raise PhotoStoreError(self.write_refusal)
        _check_filename(filename)
        key = self.key_for(filename)
        target = self.root / filename
        try:
            if target.is_file() and target.stat().st_size > 0:
                return key
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / f".{secrets.token_hex(8)}.part"
            try:
                with open(tmp, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
            if target.stat().st_size != len(data):
                raise PhotoStoreError("The photo was only partly saved.")
        except OSError as exc:
            raise PhotoStoreError(f"The photo could not be saved ({exc.strerror or type(exc).__name__}).") from None
        return key

    def locate(self, key: Optional[str]) -> Optional[pathlib.Path]:
        """The file for a key: an absolute path that exists as-is (older rows and the tests), else
        `<root>/<name>`, else the old project-root-relative spelling."""
        if key is None or not str(key).strip():
            return None
        raw = str(key).strip().replace("\\", "/")
        direct = pathlib.Path(raw)
        if direct.is_absolute() and direct.exists():
            return direct
        normalised = normalise_key(raw)
        if normalised is not None:
            candidate = self.root / normalised[len(KEY_PREFIX) + 1:]
            if candidate.exists():
                return candidate
        if not direct.is_absolute() and ".." not in direct.parts:
            legacy = PROJECT_ROOT / direct
            if legacy.exists():
                return legacy
        return None

    def load(self, key: Optional[str]) -> Optional[PhotoBlob]:
        path = self.locate(key)
        return PhotoBlob(media_type=_media_type(path.name), path=path) if path is not None else None


# ─────────────────────────────────────────────────────────────────────── cloudinary
def _default_uploader(file, **options) -> dict:
    import cloudinary.uploader
    return cloudinary.uploader.upload(file, **options)


def _default_url_builder(public_id: str, **options) -> str:
    import cloudinary.utils
    return cloudinary.utils.cloudinary_url(public_id, **options)[0]


def _default_fetcher(url: str, timeout: float) -> tuple[bytes, Optional[str]]:
    with urllib.request.urlopen(url, timeout=timeout) as response:   # noqa: S310 (https, built by the SDK)
        return response.read(), response.headers.get_content_type()


def _plain_upload_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if name == "AuthorizationRequired":
        return "Cloudinary refused the photo storage credentials."
    if name == "RateLimited":
        return "Cloudinary is limiting uploads right now. Please run the import again later."
    if name == "BadRequest":
        return "Cloudinary would not accept this image."
    return "The photo could not be uploaded to Cloudinary."


class CloudinaryPhotoStore:
    """Photos as private ("authenticated") Cloudinary images. Credentials stay on the server."""

    kind = "cloudinary"
    write_refusal: Optional[str] = None
    DELIVERY_TYPE = "authenticated"

    def __init__(self, *, cloud_name: str, api_key: str, api_secret: str,
                 folder: str = DEFAULT_CLOUDINARY_FOLDER,
                 uploader: Callable[..., dict] = _default_uploader,
                 url_builder: Callable[..., str] = _default_url_builder,
                 fetcher: Callable[[str, float], tuple[bytes, Optional[str]]] = _default_fetcher,
                 timeout: float = 30.0, cache_bytes: int = 48 * 1024 * 1024):
        self.cloud_name = cloud_name
        self._api_key = api_key
        self._api_secret = api_secret
        self.folder = folder.strip("/") or DEFAULT_CLOUDINARY_FOLDER
        self._upload = uploader
        self._url = url_builder
        self._fetch = fetcher
        self.timeout = timeout
        self._cache: "collections.OrderedDict[str, PhotoBlob]" = collections.OrderedDict()
        self._cache_size = 0
        self._cache_limit = cache_bytes
        self._lock = threading.Lock()

    def __repr__(self) -> str:                    # never print the credentials, even by accident
        return f"CloudinaryPhotoStore(cloud_name={self.cloud_name!r}, folder={self.folder!r})"

    def describe(self) -> str:
        return f"Cloudinary (cloud {self.cloud_name}, folder {self.folder})"

    def key_for(self, filename: str) -> str:
        return f"{KEY_PREFIX}/{filename}"

    def public_id(self, key: str) -> str:
        """Stable, opaque public ID for a key: the same photo always maps to the same asset."""
        normalised = normalise_key(key)
        if normalised is None:
            raise PhotoStoreError("The photo has an unusable file name.")
        return f"{self.folder}/{hashlib.sha256(normalised.encode('utf-8')).hexdigest()[:32]}"

    def save(self, filename: str, data: bytes) -> str:
        _check_filename(filename)
        key = self.key_for(filename)
        public_id = self.public_id(key)
        try:
            result = self._upload(
                io.BytesIO(data), public_id=public_id, type=self.DELIVERY_TYPE, resource_type="image",
                overwrite=False, unique_filename=False, invalidate=False, timeout=self.timeout,
                cloud_name=self.cloud_name, api_key=self._api_key, api_secret=self._api_secret)
        except Exception as exc:   # SDK and network errors alike: reported per photo, never swallowed
            logger.warning("Cloudinary upload failed for %s: %s", key, type(exc).__name__)
            raise PhotoStoreError(_plain_upload_error(exc)) from None
        if not isinstance(result, dict) or result.get("public_id") != public_id:
            logger.warning("Cloudinary upload for %s returned no matching public_id", key)
            raise PhotoStoreError("Cloudinary did not confirm the upload.")
        return key

    def delivery_url(self, key: str) -> str:
        """Signed delivery URL (server-side only). Deterministic, so nothing that expires is stored."""
        return self._url(self.public_id(key), type=self.DELIVERY_TYPE, resource_type="image", sign_url=True,
                         secure=True, cloud_name=self.cloud_name, api_secret=self._api_secret)

    def load(self, key: Optional[str]) -> Optional[PhotoBlob]:
        normalised = normalise_key(key)
        if normalised is None:
            return None
        with self._lock:
            cached = self._cache.get(normalised)
            if cached is not None:
                self._cache.move_to_end(normalised)
                return cached
        try:
            data, content_type = self._fetch(self.delivery_url(normalised), self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                logger.warning("Cloudinary photo fetch for %s failed: HTTP %s", normalised, exc.code)
            return None
        except Exception as exc:
            logger.warning("Cloudinary photo fetch for %s failed: %s", normalised, type(exc).__name__)
            return None
        if not data:
            return None
        media = content_type if content_type and content_type.startswith("image/") else _media_type(normalised)
        blob = PhotoBlob(media_type=media, data=data)
        with self._lock:
            if len(data) <= self._cache_limit:
                self._cache[normalised] = blob
                self._cache_size += len(data)
                while self._cache_size > self._cache_limit:
                    _, old = self._cache.popitem(last=False)
                    self._cache_size -= len(old.data or b"")
        return blob


# ─────────────────────────────────────────────────────────────────────── configuration
def build_store(settings: Any) -> PhotoStore:
    """The store `settings` describes. Raises PhotoStorageConfigError, in plain words, if it is wrong."""
    kind = (getattr(settings, "photo_storage", None) or "local").strip().lower()
    if kind == "cloudinary":
        values = {
            "CLOUDINARY_CLOUD_NAME": getattr(settings, "cloudinary_cloud_name", None),
            "CLOUDINARY_API_KEY": getattr(settings, "cloudinary_api_key", None),
            "CLOUDINARY_API_SECRET": getattr(settings, "cloudinary_api_secret", None),
        }
        missing = [name for name, value in values.items() if not (value or "").strip()]
        if missing:
            raise PhotoStorageConfigError(
                f"PHOTO_STORAGE is 'cloudinary' but {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set.")
        return CloudinaryPhotoStore(
            cloud_name=values["CLOUDINARY_CLOUD_NAME"].strip(), api_key=values["CLOUDINARY_API_KEY"].strip(),
            api_secret=values["CLOUDINARY_API_SECRET"].strip(),
            folder=(getattr(settings, "cloudinary_folder", None) or DEFAULT_CLOUDINARY_FOLDER))
    if kind != "local":
        raise PhotoStorageConfigError(f"PHOTO_STORAGE must be 'local' or 'cloudinary', not '{kind}'.")

    configured = (getattr(settings, "photo_storage_dir", None) or "").strip()
    if configured:
        root = pathlib.Path(configured)
        if not root.is_dir():
            raise PhotoStorageConfigError(
                f"PHOTO_STORAGE_DIR is set to {root.as_posix()}, which is not an existing folder. It is never "
                f"created automatically: an unmounted folder would silently lose every photo.")
    else:
        root = DEFAULT_LOCAL_ROOT
    refusal = RENDER_LOCAL_REFUSAL if os.environ.get("RENDER") else None
    if refusal:
        logger.critical("PHOTO STORAGE: running on Render with local photo storage. Render's disk is wiped on "
                        "every deploy, so photo imports are refused until PHOTO_STORAGE=cloudinary is set.")
    return LocalPhotoStore(root, write_refusal=refusal)


_current: Optional[PhotoStore] = None
_current_lock = threading.Lock()


def configure(settings: Any) -> PhotoStore:
    """Build the store from settings and make it the one `current_store()` returns."""
    global _current
    store = build_store(settings)
    with _current_lock:
        _current = store
    logger.info("photo storage: %s", store.describe())
    return store


def current_store() -> PhotoStore:
    global _current
    with _current_lock:
        if _current is not None:
            return _current
    from backend.config import get_settings
    return configure(get_settings())


def set_current_store(store: Optional[PhotoStore]) -> None:
    """For tests and scripts: replace (or, with None, forget) the process-wide store."""
    global _current
    with _current_lock:
        _current = store


# ─────────────────────────────────────────────────────────────────────── the resolver
def load_photo(key: Optional[str], store: Optional[PhotoStore] = None) -> Optional[PhotoBlob]:
    """THE photo resolver. None means "no photo": callers show the placeholder."""
    if key is None or not str(key).strip():
        return None
    return (store or current_store()).load(key)


def photo_response(key: Optional[str], store: Optional[PhotoStore], *, cache_control: str):
    """The HTTP response for a photo key: the photo, or the placeholder when there is none."""
    from fastapi.responses import FileResponse, Response

    headers = {"Cache-Control": cache_control}
    blob = load_photo(key, store)
    if blob is not None and blob.data is not None:
        return Response(content=blob.data, media_type=blob.media_type, headers=headers)
    if blob is not None and blob.path is not None and blob.path.is_file():
        return FileResponse(str(blob.path), media_type=blob.media_type, headers=headers)
    return FileResponse(str(PLACEHOLDER), media_type=_media_type(PLACEHOLDER.name), headers=headers)
