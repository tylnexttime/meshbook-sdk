"""Suite-wide guards.

1. **Isolation.** No test may read or write a developer's real
   ``~/.meshbook``: MESHBOOK_CONFIG_DIR points at a fresh temp dir and the
   other meshbook env vars are cleared for every test (a test that wants
   one sets it itself).

2. **Every request matches the endpoint registry.** ``meshbook.client``
   calls ``_on_request(method, url)`` for every request it sends; here
   that hook asserts the (method, path) matches an entry in
   ``meshbook._endpoints.ENDPOINTS``. Together with the AST test in
   ``test_registry.py`` (no module calls the raw ``request()`` itself)
   this is what makes the contract check unable to miss a call: anything
   the suite exercises that bypassed the registry fails here. A test that
   deliberately uses the raw escape hatch marks itself
   ``@pytest.mark.raw_request``.
"""
from __future__ import annotations

import re
import urllib.parse

import pytest

import meshbook.client as mc
from meshbook._endpoints import ENDPOINTS

_TEMPLATES = [
    (ep.method, re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(ep.path)) + "$"))
    for ep in ENDPOINTS.values()
]


def _registered(method: str, url: str) -> bool:
    path = urllib.parse.urlsplit(url).path
    return any(m == method and rx.match(path) for m, rx in _TEMPLATES)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "raw_request: the test calls client.request() on an unregistered path"
    )


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("MESHBOOK_CONFIG_DIR", str(tmp_path_factory.mktemp("meshbook-config")))
    for var in ("MESHBOOK_TOKEN", "MESHBOOK_BASE", "MESHBOOK_MESH"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _registry_guard(request, monkeypatch):
    if request.node.get_closest_marker("raw_request"):
        yield
        return
    seen: list[tuple[str, str]] = []

    def hook(method: str, url: str) -> None:
        seen.append((method, url))
        assert _registered(method, url), (
            f"{method} {urllib.parse.urlsplit(url).path} is not in meshbook._endpoints — "
            "every SDK request must go through the registry"
        )

    monkeypatch.setattr(mc, "_on_request", hook)
    yield
