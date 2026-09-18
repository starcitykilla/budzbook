"""Tests for comment photo/video attachments + server-side image resize."""
import asyncio
import io
import os
import random
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="bb_comment_resize_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "comments"), exist_ok=True)

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import app.main as main_mod  # noqa: E402
from app.db import DB_PATH, COMMENT_IMG_DIR, init_db  # noqa: E402
from app.main import app, _resize_comment_image  # noqa: E402
from app.auth import make_age_token, AGE_COOKIE  # noqa: E402

asyncio.run(init_db())
client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(AGE_COOKIE, make_age_token())


def _register_login(username):
    r = client.post("/register", data={"username": username, "password": "pw123456",
                                       "display_name": username})
    assert r.status_code in (200, 303), (username, r.status_code)
    r = client.post("/login", data={"username": username, "password": "pw123456"})
    assert r.status_code in (200, 303), (username, r.status_code)


def _make_post_id():
    r = client.post("/post", data={"body": "resize test post"})
    assert r.status_code in (200, 303), r.status_code

    async def _q():
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id FROM posts ORDER BY id DESC LIMIT 1") as cur:
                row = await cur.fetchone()
                return row[0]

    return asyncio.run(_q())


def _latest_comment_media():
    async def _q():
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT image_path, video_path FROM comments ORDER BY id DESC LIMIT 1") as cur:
                return await cur.fetchone()

    return asyncio.run(_q())


def _noise_jpeg(w, h, quality=95):
    rnd = random.Random(42)
    px = bytes(rnd.getrandbits(8) for _ in range(w * h * 3))
    im = Image.frombytes("RGB", (w, h), px)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _comment(pid, body="pic", files=None):
    return client.post(f"/post/{pid}/comment", data={"body": body},
                       files=files or {}, follow_redirects=False)


# ---- resize behavior ----
def test_large_jpeg_downscaled():
    _register_login("resizer1")
    pid = _make_post_id()
    raw = _noise_jpeg(4000, 3000)
    assert len(raw) > 1024 * 1024  # genuinely big phone-style photo
    r = _comment(pid, files={"image": ("big.jpg", raw, "image/jpeg")})
    assert r.status_code == 303, r.status_code
    image_path, _ = _latest_comment_media()
    assert image_path and image_path.startswith("comments/")
    stored = os.path.join(COMMENT_IMG_DIR, os.path.basename(image_path))
    with Image.open(stored) as sim:
        assert max(sim.size) <= 1600
        # aspect ratio preserved (4:3)
        assert sim.size[0] / sim.size[1] == pytest.approx(4 / 3, rel=0.02)
    assert os.path.getsize(stored) < len(raw)


def test_small_image_stored_byte_identical():
    _register_login("resizer2")
    pid = _make_post_id()
    im = Image.new("RGB", (800, 600), (200, 30, 30))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    raw = buf.getvalue()
    r = _comment(pid, files={"image": ("small.jpg", raw, "image/jpeg")})
    assert r.status_code == 303, r.status_code
    image_path, _ = _latest_comment_media()
    stored = os.path.join(COMMENT_IMG_DIR, os.path.basename(image_path))
    with open(stored, "rb") as f:
        assert f.read() == raw  # untouched: not upscaled, not re-encoded


def test_exif_orientation_applied_and_stripped():
    _register_login("resizer3")
    pid = _make_post_id()
    im = Image.new("RGB", (3000, 2000), (30, 200, 30))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif, quality=90)
    raw = buf.getvalue()
    r = _comment(pid, files={"image": ("exif.jpg", raw, "image/jpeg")})
    assert r.status_code == 303, r.status_code
    image_path, _ = _latest_comment_media()
    stored = os.path.join(COMMENT_IMG_DIR, os.path.basename(image_path))
    with Image.open(stored) as sim:
        # orientation applied (3000x2000 -> 2000x3000), then longest side -> 1600
        assert sim.size == (1067, 1600)
        assert not sim.info.get("exif")


def test_animated_gif_passthrough():
    _register_login("resizer4")
    pid = _make_post_id()
    frames = [Image.new("RGB", (2000, 1500), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=200, loop=0)
    raw = buf.getvalue()
    r = _comment(pid, files={"image": ("anim.gif", raw, "image/gif")})
    assert r.status_code == 303, r.status_code
    image_path, _ = _latest_comment_media()
    stored = os.path.join(COMMENT_IMG_DIR, os.path.basename(image_path))
    with open(stored, "rb") as f:
        assert f.read() == raw  # animation untouched
    with Image.open(stored) as sim:
        assert getattr(sim, "n_frames", 1) > 1


def test_png_transparency_preserved():
    _register_login("resizer5")
    pid = _make_post_id()
    im = Image.new("RGBA", (2000, 2000), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse([200, 200, 1800, 1800], fill=(255, 215, 0, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    raw = buf.getvalue()
    r = _comment(pid, files={"image": ("alpha.png", raw, "image/png")})
    assert r.status_code == 303, r.status_code
    image_path, _ = _latest_comment_media()
    stored = os.path.join(COMMENT_IMG_DIR, os.path.basename(image_path))
    with Image.open(stored) as sim:
        assert max(sim.size) <= 1600
        assert sim.mode == "RGBA"  # alpha channel kept


def test_webp_resized():
    _register_login("resizer8")
    pid = _make_post_id()
    raw = _noise_jpeg(3200, 2400)
    im = Image.open(io.BytesIO(raw))
    buf = io.BytesIO()
    im.save(buf, format="WEBP", quality=95)
    webp = buf.getvalue()
    r = _comment(pid, files={"image": ("big.webp", webp, "image/webp")})
    assert r.status_code == 303, r.status_code
    image_path, _ = _latest_comment_media()
    stored = os.path.join(COMMENT_IMG_DIR, os.path.basename(image_path))
    with Image.open(stored) as sim:
        assert max(sim.size) <= 1600


# ---- cap enforcement ----
def test_still_too_large_rejected(monkeypatch):
    monkeypatch.setattr(main_mod, "MAX_UPLOAD_BYTES", 1024)
    _register_login("resizer6")
    pid = _make_post_id()
    raw = _noise_jpeg(2000, 1500)
    r = _comment(pid, files={"image": ("big.jpg", raw, "image/jpeg")})
    assert r.status_code == 303, r.status_code
    assert "too+large" in r.headers["location"]


def test_unreadable_image_rejected():
    _register_login("resizer7")
    pid = _make_post_id()
    r = _comment(pid, files={"image": ("fake.jpg", b"not an image at all", "image/jpeg")})
    assert r.status_code == 303, r.status_code
    assert "not+readable" in r.headers["location"]


def test_bad_extension_rejected():
    _register_login("resizer9")
    pid = _make_post_id()
    r = _comment(pid, files={"image": ("evil.exe", b"MZ" * 100, "application/octet-stream")})
    assert r.status_code == 303, r.status_code
    assert "JPG%2FPNG%2FGIF%2FWebP" in r.headers["location"] or "JPG" in r.headers["location"]


# ---- unit-level ----
def test_resize_helper_never_upscales():
    im = Image.new("RGB", (400, 300), (10, 10, 10))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    out = _resize_comment_image(buf.getvalue(), ".jpg")
    assert out == buf.getvalue()


def test_resize_helper_undecodable_returns_none():
    assert _resize_comment_image(b"\x00\x01\x02\x03", ".jpg") is None


# ---- non-image paths still work ----
def test_video_upload_still_works():
    _register_login("resizer10")
    pid = _make_post_id()
    r = _comment(pid, body="clip", files={"video": ("clip.mp4", b"\x00" * 4096, "video/mp4")})
    assert r.status_code == 303, r.status_code
    _, video_path = _latest_comment_media()
    assert video_path and video_path.startswith("comments/")
    assert os.path.exists(os.path.join(COMMENT_IMG_DIR, os.path.basename(video_path)))


def test_text_only_comment_still_works():
    _register_login("resizer11")
    pid = _make_post_id()
    r = _comment(pid, body="just words")
    assert r.status_code == 303, r.status_code
