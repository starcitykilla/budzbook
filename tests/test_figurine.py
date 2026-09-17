"""Tests for the spinnable figurine pipeline.

Covers scripts/make_figurine.py (mask, polygon, outputs) on synthetic images
and the profile figurine branch (figurine div only when the asset pair
exists; flat 2D fallback otherwise). No network calls are made.
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

_tmp = tempfile.mkdtemp(prefix="bb_figurine_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "figurines"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

import make_figurine  # noqa: E402
from PIL import Image  # noqa: E402

import aiosqlite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import AGE_COOKIE, make_age_token  # noqa: E402
from app.db import DB_PATH, FIGURINE_DIR, figurine_exists, init_db  # noqa: E402
from app.main import app  # noqa: E402

asyncio.run(init_db())


def _synthetic(path, bg, fg_color, bg_mode):
    """128x128 image: solid bg with a bright/dark filled circle center."""
    img = Image.new("RGB", (128, 128), bg)
    px = img.load()
    for y in range(128):
        for x in range(128):
            if (x - 64) ** 2 + (y - 64) ** 2 < 40 ** 2:
                px[x, y] = fg_color
    img.save(path)
    return path


def _check_outputs(name, out_dir):
    size = make_figurine.OUT_SIZE
    png_p = os.path.join(out_dir, name + ".png")
    json_p = os.path.join(out_dir, name + ".json")
    mask_p = os.path.join(out_dir, name + "_mask.png")
    assert os.path.isfile(png_p) and os.path.isfile(json_p) and os.path.isfile(mask_p)

    img = Image.open(png_p)
    assert img.mode == "RGBA", "cutout must carry an alpha channel"
    alpha = img.getchannel("A")
    hist = alpha.histogram()
    transparent = sum(hist[:16])
    total = size * size
    frac = transparent / total
    assert 0.2 < frac < 0.9, "non-trivial masked area, got %.2f" % frac

    with open(json_p) as f:
        data = json.load(f)
    assert data["size"] == [size, size]
    pts = data["points"]
    assert 20 <= len(pts) <= 400, "polygon point count %d" % len(pts)
    for x, y in pts:
        assert 0 <= x <= size and 0 <= y <= size, "point out of bounds"
    # closed loop: last point near first
    x0, y0 = pts[0]
    x1, y1 = pts[-1]
    assert abs(x0 - x1) < size * 0.15 and abs(y0 - y1) < size * 0.15, "loop not closed"


def test_make_figurine_dark_bg():
    d = tempfile.mkdtemp(prefix="bb_fig_dark_")
    src = _synthetic(os.path.join(d, "src.png"), (12, 14, 16), (60, 220, 90), "dark")
    res = make_figurine.make_figurine(src, "darky", d, tol=70.0, bg="dark", close=5)
    assert res["points"] > 0
    _check_outputs("darky", d)


def test_make_figurine_light_bg():
    d = tempfile.mkdtemp(prefix="bb_fig_light_")
    src = _synthetic(os.path.join(d, "src.png"), (250, 250, 250), (40, 90, 50), "light")
    res = make_figurine.make_figurine(src, "lighty", d, tol=200.0, bg="light", close=5)
    assert res["points"] > 0
    _check_outputs("lighty", d)


def test_make_figurine_rejects_flat_image():
    d = tempfile.mkdtemp(prefix="bb_fig_flat_")
    src = os.path.join(d, "flat.png")
    Image.new("RGB", (128, 128), (128, 128, 128)).save(src)
    try:
        make_figurine.make_figurine(src, "flat", d)
    except ValueError:
        return
    raise AssertionError("expected ValueError for undetectable background")


def test_figurine_exists_helper():
    assert not figurine_exists("nobody_xyz")
    open(os.path.join(FIGURINE_DIR, "nobody_xyz.png"), "wb").write(b"x")
    assert not figurine_exists("nobody_xyz"), "needs both png and json"
    open(os.path.join(FIGURINE_DIR, "nobody_xyz.json"), "w").write("{}")
    assert figurine_exists("nobody_xyz")


# ------------------------------------------------------- profile branch
def _client():
    c = TestClient(app)
    c.cookies.set(AGE_COOKIE, make_age_token())
    return c


def _write_fake_figurine(username):
    img = Image.new("RGBA", (64, 64), (60, 220, 90, 255))
    img.save(os.path.join(FIGURINE_DIR, username + ".png"))
    poly = {"size": [64, 64],
            "points": [[32 + 20 * __import__("math").cos(a),
                        32 + 20 * __import__("math").sin(a)]
                       for a in [i * 6.2832 / 24 for i in range(24)]]}
    with open(os.path.join(FIGURINE_DIR, username + ".json"), "w") as f:
        json.dump(poly, f)


def test_profile_figurine_branch():
    c = _client()
    r = c.post("/register", data={
        "username": "figuser", "display_name": "Fig User",
        "password": "secret123", "twitch_username": "figuser"},
        follow_redirects=False)
    assert r.status_code in (200, 303)

    # no figurine assets -> no figurine div
    r = c.get("/u/figuser")
    assert r.status_code == 200
    assert 'class="figurine' not in r.text

    # with the asset pair -> spinnable figurine block + fallbacks
    _write_fake_figurine("figuser")
    r = c.get("/u/figuser")
    assert r.status_code == 200
    assert 'class="figurine' in r.text
    assert "/media/figurines/figuser.png" in r.text
    assert "/media/figurines/figuser.json" in r.text
    assert "/static/js/figurine.js" in r.text


def test_crew_copy_no_stale_3d_cta():
    c = _client()
    r = c.get("/crew")
    assert r.status_code == 200
    assert "Create your 3D avatar" not in r.text
    assert "create yours in Settings" not in r.text
    assert "/settings" in r.text
