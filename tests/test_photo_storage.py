"""backend/photo_storage.py: the local and Cloudinary photo stores, their configuration, and the ONE resolver.

No test here needs a database, a network or a Cloudinary account: the Cloudinary store is driven through
its injectable uploader / URL builder / fetcher, exactly the three places it touches the SDK and the web.
"""
import io
import pathlib
import urllib.error

import pytest
from PIL import Image

from backend import passes, photo_storage
from backend.config import Settings
from backend.photo_storage import (
    CloudinaryPhotoStore,
    LocalPhotoStore,
    PhotoStorageConfigError,
    PhotoStoreError,
    build_store,
    load_photo,
    normalise_key,
)

SECRET = "s3cr3t-never-shown"
API_KEY = "123456789012345"


def jpeg_bytes(color=(200, 30, 30), size=(60, 80)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


class FakeCloudinary:
    """Stands in for Cloudinary: remembers what was uploaded, serves it back, can be told to fail."""

    def __init__(self, fail_for=(), fail_with=None):
        self.assets: dict[str, bytes] = {}
        self.upload_calls: list[dict] = []
        self.fetches: list[str] = []
        self.url_options: list[dict] = []
        self.fail_for = set(fail_for)
        self.fail_with = fail_with or RuntimeError("connection reset")

    def uploader(self, file, **options):
        self.upload_calls.append(options)
        data = file.read()
        if any(marker in data.decode("latin-1") for marker in self.fail_for):
            raise self.fail_with
        public_id = options["public_id"]
        existing = public_id in self.assets
        if not existing or options.get("overwrite"):
            self.assets[public_id] = data
        return {"public_id": public_id, "type": options["type"], "existing": existing}

    def url_builder(self, public_id, **options):
        assert options["sign_url"] is True and options["type"] == "authenticated"
        self.url_options.append(options)
        url = f"https://res.cloudinary.test/{options['cloud_name']}/image/authenticated/s--sig--/{public_id}"
        if "crop" in options:     # a transformed copy: marked so the tests can see which one was fetched
            url += f"?t=c_{options['crop']},f_{options['fetch_format']},h_{options['height']},w_{options['width']}"
        return url

    def fetcher(self, url, timeout):
        self.fetches.append(url)
        public_id = url.split("/s--sig--/", 1)[1].split("?", 1)[0]
        if public_id not in self.assets:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return self.assets[public_id], "image/jpeg"

    def store(self, **kw) -> CloudinaryPhotoStore:
        return CloudinaryPhotoStore(cloud_name="demo-cloud", api_key=API_KEY, api_secret=SECRET,
                                    folder="convocation/student-photos", uploader=self.uploader,
                                    url_builder=self.url_builder, fetcher=self.fetcher, **kw)


@pytest.fixture(autouse=True)
def _restore_current_store():
    yield
    photo_storage.set_current_store(None)


# ─────────────────────────────────────────────────────────────── keys (the Windows-backslash rule)
@pytest.mark.parametrize("raw,expected", [
    ("photos/a b.jpg", "photos/a b.jpg"),
    ("photos\\a b.jpg", "photos/a b.jpg"),                                    # a Windows import
    ("E:\\JakobProjects\\1QR\\photos\\1_PROFILE_X.JPEG", "photos/1_PROFILE_X.JPEG"),  # absolute Windows path
    ("/app/photos/x.png", "photos/x.png"),
    ("x.png", "photos/x.png"),
    ("photos/../../etc/passwd", None),                                       # never escapes the store
    ("", None), (None, None), ("   ", None),
])
def test_normalise_key_gives_one_posix_spelling(raw, expected):
    assert normalise_key(raw) == expected


# ─────────────────────────────────────────────────────────────── local store
def test_local_store_writes_atomically_and_returns_a_posix_key(tmp_path):
    store = LocalPhotoStore(tmp_path)
    key = store.save("1_PROFILE_IMAGE_PRN_No_P1_Name_A B.jpg", b"abc")
    assert key == "photos/1_PROFILE_IMAGE_PRN_No_P1_Name_A B.jpg" and "\\" not in key
    assert (tmp_path / "1_PROFILE_IMAGE_PRN_No_P1_Name_A B.jpg").read_bytes() == b"abc"
    assert [p.name for p in tmp_path.iterdir()] == ["1_PROFILE_IMAGE_PRN_No_P1_Name_A B.jpg"]  # no temp file left


def test_local_store_never_replaces_an_existing_photo(tmp_path):
    store = LocalPhotoStore(tmp_path)
    store.save("a.jpg", b"first")
    store.save("a.jpg", b"second")
    assert (tmp_path / "a.jpg").read_bytes() == b"first"


def test_local_store_reports_a_failed_write_instead_of_swallowing_it(tmp_path, monkeypatch):
    store = LocalPhotoStore(tmp_path)

    def broken_replace(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(photo_storage.os, "replace", broken_replace)
    with pytest.raises(PhotoStoreError, match="could not be saved"):
        store.save("a.jpg", b"abc")
    assert list(tmp_path.iterdir()) == []                                    # no half-written file under any name


def test_local_store_refuses_bad_file_names(tmp_path):
    for bad in ("../x.jpg", "a/b.jpg", "a\\b.jpg", "", ".."):
        with pytest.raises(PhotoStoreError):
            LocalPhotoStore(tmp_path).save(bad, b"x")


def test_local_store_with_a_write_refusal_writes_nothing(tmp_path):
    store = LocalPhotoStore(tmp_path, write_refusal=photo_storage.RENDER_LOCAL_REFUSAL)
    with pytest.raises(PhotoStoreError, match="no permanent photo storage"):
        store.save("a.jpg", b"x")
    assert list(tmp_path.iterdir()) == []


def test_local_resolver_finds_every_existing_spelling(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"A")
    store = LocalPhotoStore(tmp_path)
    for key in ("photos/a.jpg", "photos\\a.jpg", "C:\\old\\machine\\photos\\a.jpg", str(tmp_path / "a.jpg")):
        blob = store.load(key)
        assert blob is not None and blob.path.read_bytes() == b"A" and blob.media_type == "image/jpeg", key
    assert store.load("photos/missing.jpg") is None and store.load(None) is None


# ─────────────────────────────────────────────────────────────── cloudinary store (mocked)
def test_cloudinary_upload_is_private_idempotent_and_opaque():
    fake = FakeCloudinary()
    store = fake.store()
    key = store.save("7_PROFILE_IMAGE_PRN_No_202250128037_Name_Akash Shirsath.jpg", b"photo")
    assert key == "photos/7_PROFILE_IMAGE_PRN_No_202250128037_Name_Akash Shirsath.jpg"   # same key format as local
    call = fake.upload_calls[0]
    assert call["type"] == "authenticated" and call["overwrite"] is False and call["resource_type"] == "image"
    public_id = call["public_id"]
    assert public_id.startswith("convocation/student-photos/")
    assert "202250128037" not in public_id and "Akash" not in public_id                   # no PRN or name at Cloudinary
    assert store.public_id(key) == public_id == store.public_id(key.replace("/", "\\"))   # stable, any spelling

    store.save("7_PROFILE_IMAGE_PRN_No_202250128037_Name_Akash Shirsath.jpg", b"a different photo")
    assert len(fake.assets) == 1 and fake.assets[public_id] == b"photo"                  # re-run: never replaced


def test_cloudinary_failure_is_a_plain_per_photo_error():
    fake = FakeCloudinary(fail_for={"BOOM"})
    store = fake.store()
    with pytest.raises(PhotoStoreError) as caught:
        store.save("a.jpg", b"BOOM")
    assert str(caught.value) == "The photo could not be uploaded to Cloudinary."
    assert SECRET not in str(caught.value)


def test_cloudinary_unconfirmed_upload_is_not_treated_as_success():
    store = CloudinaryPhotoStore(cloud_name="c", api_key="k", api_secret=SECRET,
                                 uploader=lambda f, **o: {"public_id": "something-else"})
    with pytest.raises(PhotoStoreError, match="did not confirm"):
        store.save("a.jpg", b"x")


def test_cloudinary_credentials_never_appear_in_repr_or_description():
    store = FakeCloudinary().store()
    assert SECRET not in repr(store) and API_KEY not in repr(store)
    assert SECRET not in store.describe() and API_KEY not in store.describe()


def test_cloudinary_resolver_fetches_server_side_and_caches():
    fake = FakeCloudinary()
    store = fake.store()
    key = store.save("a.jpg", b"\xff\xd8photo")
    first, second = store.load(key), store.load("photos\\a.jpg")
    assert first.data == b"\xff\xd8photo" and first.media_type == "image/jpeg"
    assert second is first and len(fake.fetches) == 1                                     # the LED reuses it
    assert store.load("photos/never-uploaded.jpg") is None                               # 404 -> no photo


def test_cloudinary_resolver_survives_a_network_error():
    def down(url, timeout):
        raise TimeoutError("timed out")
    store = CloudinaryPhotoStore(cloud_name="c", api_key="k", api_secret=SECRET, fetcher=down,
                                 url_builder=lambda pid, **o: "https://x/" + pid)
    assert store.load("photos/a.jpg") is None


def test_real_sdk_builds_a_signed_authenticated_url_without_the_secret():
    """The one real-SDK check: the URL the server fetches is signed, private-type, and carries no secret."""
    store = CloudinaryPhotoStore(cloud_name="demo-cloud", api_key=API_KEY, api_secret=SECRET)
    url = store.delivery_url("photos/a.jpg")
    assert url.startswith("https://res.cloudinary.com/demo-cloud/image/authenticated/s--")
    assert store.public_id("photos/a.jpg") in url and SECRET not in url and API_KEY not in url
    assert url == store.delivery_url("photos/a.jpg")                                    # deterministic: nothing expires


# ─────────────────────────────────────────────────────────────── configuration fails loudly
def test_default_is_local_and_needs_no_cloudinary(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    store = build_store(Settings(photo_storage="local", photo_storage_dir=None))
    assert store.kind == "local" and store.root == photo_storage.DEFAULT_LOCAL_ROOT and store.write_refusal is None


@pytest.mark.parametrize("missing", ["cloudinary_cloud_name", "cloudinary_api_key", "cloudinary_api_secret"])
def test_cloudinary_with_a_missing_credential_refuses_to_start(missing):
    values = {"cloudinary_cloud_name": "c", "cloudinary_api_key": "k", "cloudinary_api_secret": SECRET, missing: ""}
    with pytest.raises(PhotoStorageConfigError) as caught:
        build_store(Settings(photo_storage="cloudinary", **values))
    assert missing.upper() in str(caught.value) and SECRET not in str(caught.value)


def test_cloudinary_with_all_credentials_builds_the_cloudinary_store():
    store = build_store(Settings(photo_storage="cloudinary", cloudinary_cloud_name="c", cloudinary_api_key="k",
                                 cloudinary_api_secret=SECRET))
    assert store.kind == "cloudinary" and store.folder == "convocation/student-photos"


def test_unknown_storage_kind_refuses_to_start():
    with pytest.raises(PhotoStorageConfigError, match="must be 'local' or 'cloudinary'"):
        build_store(Settings(photo_storage="s3"))


def test_a_configured_folder_that_does_not_exist_is_never_created(tmp_path):
    """The missing-Docker-volume bug: an unmounted folder must stop the server, not silently fill up."""
    target = tmp_path / "not-mounted"
    with pytest.raises(PhotoStorageConfigError, match="never created automatically"):
        build_store(Settings(photo_storage_dir=str(target)))
    assert not target.exists()


def test_local_storage_on_render_refuses_writes_but_still_starts(monkeypatch, tmp_path):
    monkeypatch.setenv("RENDER", "true")
    store = build_store(Settings(photo_storage="local", photo_storage_dir=str(tmp_path)))
    assert store.write_refusal == photo_storage.RENDER_LOCAL_REFUSAL


def test_the_app_will_not_start_with_broken_photo_storage():
    from backend.main import create_app
    with pytest.raises(PhotoStorageConfigError):
        create_app(Settings(photo_storage="cloudinary"))


# ─────────────────────────────────────────────────────────────── one resolver for every consumer
def test_pass_pdf_photo_comes_through_the_resolver_from_cloudinary():
    fake = FakeCloudinary()
    store = fake.store()
    key = store.save("p.jpg", jpeg_bytes())
    photo_storage.set_current_store(store)
    jpeg, warning = passes._prepare_photo(key)
    assert warning is None and jpeg[:2] == b"\xff\xd8"
    assert passes._prepare_photo("photos/not-uploaded.jpg") == (None, "NO_PHOTO")


def test_pass_pdf_photo_from_the_local_store(tmp_path):
    (tmp_path / "p.jpg").write_bytes(jpeg_bytes())
    photo_storage.set_current_store(LocalPhotoStore(tmp_path))
    jpeg, warning = passes._prepare_photo("photos\\p.jpg")
    assert warning is None and jpeg[:2] == b"\xff\xd8"


def test_load_photo_uses_the_current_store_when_none_is_given(tmp_path):
    (tmp_path / "a.png").write_bytes(b"png")
    photo_storage.set_current_store(LocalPhotoStore(tmp_path))
    assert load_photo("photos/a.png").media_type == "image/png"
    assert load_photo(None) is None and load_photo("") is None


def test_no_other_module_builds_photo_paths_itself():
    """The resolver is the only place that turns a photo key into a file: the old copies are gone."""
    root = pathlib.Path(__file__).resolve().parents[1] / "backend"
    for name in ("engine/routes.py", "passes.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "photo_storage.photo_response(" in source or "photo_storage.load_photo(" in source, name
        assert "pathlib.Path(row[" not in source and "_PROJECT_ROOT / source" not in source, name
        assert 'photo_path"].replace(' not in source, name


# ─────────────────────────────────────────────────────────────── the pass-sized Cloudinary copy
PASS_TX = "c_limit,f_jpg,h_944,w_756"


def test_pass_variant_is_twice_the_print_size_and_only_shrinks():
    tx = photo_storage.CLOUDINARY_VARIANTS[photo_storage.PASS_VARIANT]
    assert (tx["width"], tx["height"]) == (passes.PHOTO_PX[0] * 2, passes.PHOTO_PX[1] * 2)
    assert tx["crop"] == "limit" and tx["fetch_format"] == "jpg"          # never crops, never enlarges


def test_real_sdk_signs_the_pass_transformation():
    """The transformation is part of what the SDK signs: the signature differs from the original's, and the
    URL is still the private, signed, secret-free kind."""
    store = CloudinaryPhotoStore(cloud_name="demo-cloud", api_key=API_KEY, api_secret=SECRET)
    original = store.delivery_url("photos/a.jpg")
    small = store.delivery_url("photos/a.jpg", photo_storage.PASS_VARIANT)
    assert small.startswith("https://res.cloudinary.com/demo-cloud/image/authenticated/s--")
    assert f"/{PASS_TX}/" in small and PASS_TX not in original
    assert store.public_id("photos/a.jpg") in small and SECRET not in small and API_KEY not in small
    signature = lambda url: url.split("/authenticated/", 1)[1].split("/", 1)[0]
    assert signature(small) != signature(original)
    other = CloudinaryPhotoStore(cloud_name="demo-cloud", api_key=API_KEY, api_secret="another-secret")
    assert signature(other.delivery_url("photos/a.jpg", photo_storage.PASS_VARIANT)) != signature(small)


def test_pass_generation_requests_the_transformed_copy():
    fake = FakeCloudinary()
    store = fake.store()
    key = store.save("p.jpg", jpeg_bytes())
    photo_storage.set_current_store(store)
    jpeg, warning = passes._prepare_photo(key)
    assert warning is None and jpeg[:2] == b"\xff\xd8"
    assert len(fake.fetches) == 1 and fake.fetches[0].endswith("?t=" + PASS_TX)
    assert fake.url_options[-1]["crop"] == "limit" and fake.url_options[-1]["width"] == 756


def test_other_photo_consumers_still_request_the_original():
    """/photo, the Stage slots and the LED all go through photo_response / load_photo with no variant."""
    fake = FakeCloudinary()
    store = fake.store()
    key = store.save("a.jpg", b"\xff\xd8photo")
    photo_storage.set_current_store(store)
    assert load_photo(key).data == b"\xff\xd8photo"
    fresh = fake.store()                                                 # empty cache: photo_response must fetch
    response = photo_storage.photo_response("photos/a.jpg", fresh, cache_control="no-store")
    assert response.body == b"\xff\xd8photo"
    assert len(fake.fetches) == 2 and not any("?t=" in url for url in fake.fetches)
    assert all("crop" not in options for options in fake.url_options)


def test_cache_keeps_the_original_and_the_pass_copy_apart():
    fake = FakeCloudinary()
    store = fake.store()
    key = store.save("a.jpg", b"\xff\xd8original")
    original = store.load(key)
    small = store.load(key, variant=photo_storage.PASS_VARIANT)
    assert small is not original and len(fake.fetches) == 2               # the pass never reuses the original
    assert fake.fetches[0].endswith(store.public_id(key)) and fake.fetches[1].endswith("?t=" + PASS_TX)
    assert store.load(key) is original                                    # each is cached under its own key
    assert store.load("photos\\a.jpg", variant=photo_storage.PASS_VARIANT) is small
    assert len(fake.fetches) == 2
    assert set(store._cache) == {("photos/a.jpg", None), ("photos/a.jpg", photo_storage.PASS_VARIANT)}


def test_local_store_ignores_the_pass_variant(tmp_path):
    (tmp_path / "p.jpg").write_bytes(jpeg_bytes())
    store = LocalPhotoStore(tmp_path)
    plain, for_pass = store.load("photos/p.jpg"), store.load("photos\\p.jpg", variant=photo_storage.PASS_VARIANT)
    assert plain == for_pass and plain.path == tmp_path / "p.jpg" and plain.data is None
    assert store.load("photos/missing.jpg", variant=photo_storage.PASS_VARIANT) is None


def test_pass_pdf_renders_from_the_transformed_cloudinary_copy():
    fake = FakeCloudinary()
    store = fake.store()
    key = store.save("p.jpg", jpeg_bytes(size=(756, 944)))
    photo_storage.set_current_store(store)
    data = [passes.PassData(name=f"Student {i}", prn=f"PRN{i}", programme="B.Sc.", token="A" * 32,
                            photo_path=key if i < 3 else "photos/not-uploaded.jpg") for i in range(5)]
    sheets = passes.render_sheets(data, "Convocation")
    single = passes.render_single(data[0], "Convocation")
    assert sheets.pdf.startswith(b"%PDF") and single.pdf.startswith(b"%PDF") and sheets.count == 5
    assert [(w.prn, w.code) for w in sheets.warnings] == [("PRN3", "NO_PHOTO"), ("PRN4", "NO_PHOTO")]
    assert fake.fetches and all(url.endswith("?t=" + PASS_TX) for url in fake.fetches)
