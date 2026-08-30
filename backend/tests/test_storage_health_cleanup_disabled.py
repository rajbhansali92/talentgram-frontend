"""P0.5 (Cloudinary rearchitecture) — the destructive one-click storage
cleanup endpoint must be permanently inert.

`POST /api/admin/cloudinary/health/cleanup` used to mass-`destroy()` every
Cloudinary resource missing from an incomplete reference heuristic, plus
collection-wide `$pull` against historical media arrays. It is now disabled:
it must return HTTP 410 and perform NO reads, deletes, or mutations.

The tests are synchronous and fully self-contained — no module-level state is
mutated, so nothing leaks into the rest of this fragile suite's shared mocks.
The handler raises before its first `await`, so the coroutine is driven one
step by hand rather than via an event loop.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath("backend"))

from routers import cloudinary_admin
from routers.cloudinary_admin import run_storage_cleanup, router


def _drive_to_raise(coro):
    """Advance a coroutine expected to raise before its first await."""
    try:
        coro.send(None)
    finally:
        coro.close()


def test_health_cleanup_returns_410_and_touches_nothing():
    db_mock = MagicMock(name="cloudinary_admin.db")
    destroy = MagicMock()
    delete_resources = MagicMock()
    cleanup_media = AsyncMock()
    r2_client = MagicMock()
    providers = AsyncMock()
    list_cld = MagicMock()
    list_r2 = MagicMock()
    log_action = AsyncMock()

    with patch.object(cloudinary_admin, "db", db_mock), \
         patch.object(cloudinary_admin.cloudinary.uploader, "destroy", destroy), \
         patch.object(cloudinary_admin.cloudinary.api, "delete_resources", delete_resources), \
         patch.object(cloudinary_admin, "cleanup_media_storage", cleanup_media), \
         patch.object(cloudinary_admin, "get_r2_client", lambda: r2_client), \
         patch.object(cloudinary_admin, "assert_providers_healthy", providers), \
         patch.object(cloudinary_admin, "list_cloudinary_physical_resources_sync", list_cld), \
         patch.object(cloudinary_admin, "list_r2_physical_objects_sync", list_r2), \
         patch.object(cloudinary_admin, "log_storage_action", log_action):

        with pytest.raises(HTTPException) as exc:
            _drive_to_raise(
                run_storage_cleanup(admin={"id": "a1", "email": "admin@example.com", "role": "admin"})
            )

    assert exc.value.status_code == 410
    assert "permanently disabled" in exc.value.detail.lower()

    # Nothing destructive — and nothing at all — was invoked.
    assert db_mock.mock_calls == []
    destroy.assert_not_called()
    delete_resources.assert_not_called()
    cleanup_media.assert_not_called()
    r2_client.delete_object.assert_not_called()
    providers.assert_not_called()
    list_cld.assert_not_called()
    list_r2.assert_not_called()
    log_action.assert_not_called()


def test_health_cleanup_route_registered_as_deprecated():
    route = next(
        (r for r in router.routes
         if getattr(r, "path", None) == "/api/admin/cloudinary/health/cleanup"),
        None,
    )
    assert route is not None, "route should stay registered so stale clients get 410, not 404"
    assert "POST" in route.methods
    assert route.deprecated is True
