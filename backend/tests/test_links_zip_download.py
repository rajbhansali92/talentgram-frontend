"""Client-View UX Sprint — Download Folder ZIP reliability.

Covers the new bounded-concurrency, retry-once fetch helper used by both
download_talent_zip and download_campaign_bundle_zip (backend/routers/links.py).
The existing hard-fail-on-missing-media policy (a deliberate prior product
decision, documented as "C2" in links.py) is intentionally NOT changed by
this sprint — only reliability (retry) and speed (parallel fetch) improved —
so no test here asserts a partial-ZIP-ships-anyway behavior.
"""
import asyncio
import os
import sys

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "dummy")
os.environ.setdefault("ADMIN_EMAIL", "admin@talentgram.co")
os.environ.setdefault("ADMIN_PASSWORD", "dummy")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "talentgram")
os.environ.setdefault("CLOUDINARY_API_KEY", "dummy")
os.environ.setdefault("CLOUDINARY_API_SECRET", "dummy")

sys.path.insert(0, os.path.abspath("backend"))

from routers import links as links_router  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code, chunks):
        self.status_code = status_code
        self._chunks = chunks

    async def aiter_bytes(self, chunk_size=65536):
        for c in self._chunks:
            yield c


class _FakeStreamCtx:
    """Async context manager mimicking httpx.AsyncClient.stream()'s return value."""
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc

    async def __aenter__(self):
        if self._raise_exc:
            raise self._raise_exc
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """Fake httpx.AsyncClient — .stream() returns a scripted sequence of
    outcomes (one per call), so a test can simulate "fails once, then
    succeeds" without any real network I/O."""
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)  # list of ("ok", [b"..."]) | ("http_error", code) | ("raise", Exception)
        self.call_count = 0

    def stream(self, method, url):
        outcome = self._outcomes[min(self.call_count, len(self._outcomes) - 1)]
        self.call_count += 1
        kind = outcome[0]
        if kind == "ok":
            return _FakeStreamCtx(response=_FakeResponse(200, outcome[1]))
        if kind == "http_error":
            return _FakeStreamCtx(response=_FakeResponse(outcome[1], []))
        if kind == "raise":
            return _FakeStreamCtx(raise_exc=outcome[1])
        raise AssertionError(f"unknown outcome kind: {kind}")


@pytest.mark.asyncio
async def test_fetch_zip_item_bytes_success_first_try():
    client = _FakeClient([("ok", [b"hello ", b"world"])])
    sem = asyncio.Semaphore(4)
    filename, data, err = await links_router._fetch_zip_item_bytes(client, sem, "a.jpg", "https://x/a.jpg")
    assert filename == "a.jpg"
    assert data == b"hello world"
    assert err is None
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_fetch_zip_item_bytes_retries_once_then_succeeds():
    client = _FakeClient([
        ("http_error", 503),
        ("ok", [b"recovered"]),
    ])
    sem = asyncio.Semaphore(4)
    filename, data, err = await links_router._fetch_zip_item_bytes(client, sem, "b.mp4", "https://x/b.mp4")
    assert filename == "b.mp4"
    assert data == b"recovered"
    assert err is None
    assert client.call_count == 2  # confirms the retry actually happened


@pytest.mark.asyncio
async def test_fetch_zip_item_bytes_retry_exhausted_fails_cleanly():
    client = _FakeClient([
        ("raise", TimeoutError("connect timed out")),
        ("raise", TimeoutError("connect timed out")),
    ])
    sem = asyncio.Semaphore(4)
    filename, data, err = await links_router._fetch_zip_item_bytes(client, sem, "c.mp4", "https://x/c.mp4")
    assert filename == "c.mp4"
    assert data is None
    assert err is not None
    assert "timed out" in err.lower()
    assert client.call_count == 2  # both attempts were made, never raises


@pytest.mark.asyncio
async def test_fetch_zip_item_bytes_never_raises_on_persistent_http_error():
    client = _FakeClient([("http_error", 404), ("http_error", 404)])
    sem = asyncio.Semaphore(4)
    filename, data, err = await links_router._fetch_zip_item_bytes(client, sem, "d.jpg", "https://x/d.jpg")
    assert data is None
    assert err == "HTTP 404"


@pytest.mark.asyncio
async def test_fetch_zip_item_bytes_respects_semaphore_bound():
    # 6 concurrent fetches through a semaphore of 2 — assert peak concurrency
    # never exceeds the bound.
    sem = asyncio.Semaphore(2)
    peak = 0
    current = 0
    lock = asyncio.Lock()

    class _SlowClient:
        def stream(self, method, url):
            return _SlowCtx()

    class _SlowCtx:
        async def __aenter__(self):
            nonlocal peak, current
            async with lock:
                current += 1
                peak = max(peak, current)
            await asyncio.sleep(0.02)
            return _FakeResponse(200, [b"x"])

        async def __aexit__(self, *exc):
            nonlocal current
            async with lock:
                current -= 1
            return False

    client = _SlowClient()
    await asyncio.gather(*[
        links_router._fetch_zip_item_bytes(client, sem, f"item{i}.jpg", f"https://x/{i}")
        for i in range(6)
    ])
    assert peak <= 2
