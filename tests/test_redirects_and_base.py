"""Critique C-06: redirects never carry credentials off-origin or turn a
write into a GET, and the base must be https (loopback excepted).

These tests run a REAL loopback HTTP server (127.0.0.1, ephemeral ports)
so the SDK's own opener and redirect handler are exercised end to end;
nothing leaves the machine.
"""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from meshbook import MeshbookClient, MeshbookError
from tests._fakes import MESH


class _Server:
    def __init__(self, handler_fn):
        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                seen.append((self.command, self.path, dict(self.headers), body))
                status, headers, payload = handler_fn(self.command, self.path)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                data = json.dumps(payload).encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = _any

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers():
    made = []

    def make(fn):
        s = _Server(fn)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def _client(base, tmp_path):
    return MeshbookClient(token="mb_token_secret", mesh=MESH, base=base,
                          config_path=tmp_path / "none", max_attempts=1)


def test_cross_origin_redirect_is_refused_and_leaks_nothing(servers, tmp_path):
    thief = servers(lambda m, p: (200, {}, {"ok": True, "data": {"items": []}}))
    origin = servers(lambda m, p: (301, {"Location": thief.url + p}, {}))
    with pytest.raises(MeshbookError) as ei:
        _client(origin.url, tmp_path).contacts.list()
    assert ei.value.code == "redirect_refused" and thief.url in ei.value.message
    assert thief.seen == []  # the bearer never reached the other origin


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirected_write_is_refused_never_downgraded(servers, tmp_path, status):
    def fn(m, p):
        if p.startswith("/moved"):
            return 200, {}, {"ok": True, "data": {"items": [{"id": "x"}]}}
        return status, {"Location": "/moved" + p}, {}

    srv = servers(fn)
    with pytest.raises(MeshbookError) as ei:
        _client(srv.url, tmp_path).chat.post("hello")
    assert ei.value.code == "redirect_refused"
    assert [s[0] for s in srv.seen] == ["POST"]  # never re-sent as a GET


def test_same_origin_get_redirect_is_followed(servers, tmp_path):
    def fn(m, p):
        if p.startswith("/api/contacts2"):
            return 200, {}, {"ok": True, "data": {"items": [{"id": "c"}], "total": 1}}
        return 302, {"Location": "/api/contacts2"}, {}

    srv = servers(fn)
    rows = _client(srv.url, tmp_path).contacts.list()
    assert rows == [{"id": "c"}]
    assert srv.seen[1][2].get("Authorization") == "Bearer mb_token_secret"


@pytest.mark.parametrize(
    "base",
    ["http://meshbook.org", "http://192.168.1.10:8000", "ftp://meshbook.org", "meshbook.org",
     "https://"],
)
def test_insecure_or_malformed_base_is_refused(base, tmp_path):
    with pytest.raises(MeshbookError) as ei:
        MeshbookClient(token="t", base=base, config_path=tmp_path / "none")
    assert ei.value.code == "insecure_base"


def test_insecure_base_from_env_or_config_is_refused_too(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHBOOK_BASE", "http://evil.example")
    with pytest.raises(MeshbookError) as ei:
        MeshbookClient(token="t", config_path=tmp_path / "none")
    assert ei.value.code == "insecure_base"


@pytest.mark.parametrize(
    "base",
    ["https://meshbook.org/", "http://localhost:8000", "http://127.0.0.1:9", "http://[::1]:8000",
     "http://dev.localhost"],
)
def test_https_and_loopback_bases_are_accepted(base, tmp_path):
    assert MeshbookClient(token="t", base=base, config_path=tmp_path / "none").base == \
        base.rstrip("/")
