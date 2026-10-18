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

3. **Every query parameter is one the route declares** in the pinned
   spec (``tests/contract/openapi.json``). FastAPI drops unknown query
   parameters without a word, so a misspelt or unsupported one is a
   filter that silently does nothing (0.4.0 sent ``entityType`` to
   ``/api/saved-views``, which reads ``module``, and ``offset`` to routes
   that page by ``before``). ``limit``/``offset`` are allowed on a route
   that declares no ``limit`` at all: the client pages those itself.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

import pytest

import meshbook.client as mc
from meshbook._endpoints import ENDPOINTS, Endpoint

_TEMPLATES = [
    (ep, re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(ep.path)) + "$"))
    for ep in ENDPOINTS.values()
]
_SPEC = json.loads(
    (Path(__file__).parent / "contract" / "openapi.json").read_text(encoding="utf-8")
)


def _match(method: str, url: str) -> Endpoint | None:
    path = urllib.parse.urlsplit(url).path
    for ep, rx in _TEMPLATES:
        if ep.method == method and rx.match(path):
            return ep
    return None


def _registered(method: str, url: str) -> bool:
    return _match(method, url) is not None


def _undeclared_query(method: str, url: str) -> list[str]:
    ep = _match(method, url)
    if ep is None or ep.pending:
        return []
    op = _SPEC["paths"].get(ep.path, {}).get(ep.method.lower(), {})
    declared = {p["name"] for p in op.get("parameters", []) if p.get("in") == "query"}
    emulated = {"limit", "offset"} if "limit" not in declared else set()
    sent = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return sorted(k for k in sent if k not in declared and k not in emulated)


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
        extra = _undeclared_query(method, url)
        assert not extra, (
            f"{method} {urllib.parse.urlsplit(url).path} sent query {extra} that the pinned "
            "spec does not declare for it: the server would silently ignore them"
        )

    monkeypatch.setattr(mc, "_on_request", hook)
    yield
