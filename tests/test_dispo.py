"""Tests for the Find a Dispo page (Google Places)."""
import asyncio
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="bb_dispo_test_")
os.environ["THC_SOCIAL_DB"] = os.path.join(_tmp, "test.db")
os.environ["THC_SOCIAL_MEDIA"] = os.path.join(_tmp, "media")
os.makedirs(os.path.join(_tmp, "media", "avatars"), exist_ok=True)
os.makedirs(os.path.join(_tmp, "media", "posts"), exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import make_age_token, AGE_COOKIE  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

asyncio.run(init_db())
client = TestClient(app, raise_server_exceptions=False)
client.cookies.set(AGE_COOKIE, make_age_token())

anon = TestClient(app, raise_server_exceptions=False)
anon.cookies.set(AGE_COOKIE, make_age_token())


def _register(username, password="pw123456"):
    r = client.post("/register", data={"username": username, "password": password,
                                       "display_name": username,
        "agree_terms": "yes"},
                    follow_redirects=False)
    assert r.status_code in (200, 303), (username, r.status_code)


def _login(username, password="pw123456"):
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=False)
    assert r.status_code in (200, 303), (username, r.status_code)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        _FakeClient.calls += 1
        assert params["key"] == "fake-key"
        if "nearbysearch" in url:
            assert params["keyword"] == "cannabis dispensary"
            assert params["rankby"] == "distance"
            return _FakeResp({
                "status": "OK",
                "results": [
                    {"name": "Far Dispo", "vicinity": "200 Main St",
                     "rating": 4.0, "user_ratings_total": 50,
                     "opening_hours": {"open_now": False},
                     "place_id": "pid-far",
                     "geometry": {"location": {"lat": 37.10, "lng": -75.0}}},
                    {"name": "Near Dispo", "vicinity": "100 Main St",
                     "rating": 4.8, "user_ratings_total": 212,
                     "opening_hours": {"open_now": True},
                     "place_id": "pid-near",
                     "geometry": {"location": {"lat": 37.01, "lng": -75.0}}},
                ]})
        if "geocode" in url:
            return _FakeResp({
                "results": [{
                    "formatted_address": "Saxis, VA 23427, USA",
                    "geometry": {"location": {"lat": 37.93, "lng": -75.72}},
                }]})
        raise AssertionError(f"unexpected url {url}")


# ---- access control ----
def test_dispo_page_requires_login():
    r = anon.get("/dispo", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_dispo_api_requires_login():
    r = anon.get("/api/dispo/search?lat=37.0&lng=-75.0", follow_redirects=False)
    assert r.status_code == 303
    r = anon.get("/api/dispo/geocode?q=23427", follow_redirects=False)
    assert r.status_code == 303


# ---- page ----
def test_dispo_page_setup_note_without_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    _register("dispogal")
    _login("dispogal")
    r = client.get("/dispo")
    assert r.status_code == 200
    assert "being set up" in r.text
    assert "Use my location" not in r.text


def test_dispo_page_configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    _login("dispogal")
    r = client.get("/dispo")
    assert r.status_code == 200
    assert "Use my location" in r.text
    assert "dispo-manual" in r.text


# ---- search route ----
def test_dispo_search_503_without_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    _login("dispogal")
    r = client.get("/api/dispo/search?lat=37.0&lng=-75.0")
    assert r.status_code == 503


def test_dispo_search_proxied_sorted_by_distance(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    monkeypatch.setattr("app.main.httpx.AsyncClient", _FakeClient)
    _FakeClient.calls = 0
    _login("dispogal")
    r = client.get("/api/dispo/search?lat=37.0&lng=-75.0")
    assert r.status_code == 200
    results = r.json()["results"]
    assert [d["name"] for d in results] == ["Near Dispo", "Far Dispo"]
    near = results[0]
    assert near["address"] == "100 Main St"
    assert near["rating"] == 4.8
    assert near["ratings_total"] == 212
    assert near["open_now"] is True
    assert near["place_id"] == "pid-near"
    assert near["distance_mi"] is not None
    assert 0.5 < near["distance_mi"] < 1.0
    assert results[1]["distance_mi"] > near["distance_mi"]
    assert _FakeClient.calls == 1


def test_dispo_search_cached(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    monkeypatch.setattr("app.main.httpx.AsyncClient", _FakeClient)
    _FakeClient.calls = 0
    _login("dispogal")
    # distinct coords so the shared in-memory cache can't collide with other tests
    r1 = client.get("/api/dispo/search?lat=38.0&lng=-76.0")
    r2 = client.get("/api/dispo/search?lat=38.001&lng=-76.001")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert _FakeClient.calls == 1


def test_dispo_search_invalid_coords(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    _login("dispogal")
    assert client.get("/api/dispo/search?lat=nope&lng=-75.0").status_code == 400
    assert client.get("/api/dispo/search?lat=97.0&lng=-75.0").status_code == 400


# ---- geocode route ----
def test_dispo_geocode_503_without_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    _login("dispogal")
    r = client.get("/api/dispo/geocode?q=23427")
    assert r.status_code == 503


def test_dispo_geocode_proxied(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    monkeypatch.setattr("app.main.httpx.AsyncClient", _FakeClient)
    _login("dispogal")
    r = client.get("/api/dispo/geocode?q=23427")
    assert r.status_code == 200
    body = r.json()
    assert body["lat"] == 37.93
    assert body["lng"] == -75.72
    assert body["formatted"] == "Saxis, VA 23427, USA"


def test_dispo_geocode_empty_query(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake-key")
    _login("dispogal")
    assert client.get("/api/dispo/geocode?q=").status_code == 400
