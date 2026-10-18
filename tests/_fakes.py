"""A routing fake transport for the 0.4 tests (no network, ever).

``Router`` replaces ``meshbook.client._urlopen``. Routes are matched by
METHOD and URL path (query ignored); each route replays its responses in
order and the last one sticks. A response is a JSON-able payload, raw
``bytes``, or :class:`Err` (raised as ``urllib.error.HTTPError``).
Unrouted requests fail loudly, so a test can never pass by accident on a
call it did not expect.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

MESH = "11111111-1111-1111-1111-111111111111"
MESH2 = "22222222-2222-2222-2222-222222222222"
U1 = "aaaaaaaa-0000-0000-0000-000000000001"
U2 = "aaaaaaaa-0000-0000-0000-000000000002"
T1 = "bbbbbbbb-0000-0000-0000-000000000001"
P1 = "cccccccc-0000-0000-0000-000000000001"
C1 = "dddddddd-0000-0000-0000-000000000001"
E1 = "eeeeeeee-0000-0000-0000-000000000001"
A1 = "ffffffff-0000-0000-0000-000000000001"


@dataclass
class Err:
    status: int
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)


class Resp:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Resp:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def ok_list(items: list[Any], total: int | None = None, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"items": items, "total": len(items) if total is None else total,
                                 **extra}}


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, list[Any]]] = []
        self.requests: list[Any] = []

    def add(self, method: str, path: str, *responses: Any) -> Router:
        self.routes.append((method.upper(), path, list(responses) or [ok({})]))
        return self

    def __call__(self, req: Any, timeout: float) -> Resp:
        self.requests.append(req)
        path = urllib.parse.urlsplit(req.get_full_url()).path
        for method, rpath, responses in self.routes:
            if method == req.get_method() and rpath == path:
                resp = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(resp, Err):
                    body = json.dumps(resp.payload if resp.payload is not None else {}).encode()
                    raise urllib.error.HTTPError(
                        req.get_full_url(), resp.status, "err", resp.headers, io.BytesIO(body)
                    )
                if isinstance(resp, Resp):
                    return resp
                if isinstance(resp, bytes):
                    return Resp(resp)
                return Resp(json.dumps(resp).encode())
        raise AssertionError(f"unrouted request: {req.get_method()} {req.get_full_url()}")

    # ── inspection helpers ──
    def calls(self, method: str | None = None, path: str | None = None) -> list[Any]:
        out = []
        for r in self.requests:
            p = urllib.parse.urlsplit(r.get_full_url()).path
            if (method is None or r.get_method() == method) and (path is None or p == path):
                out.append(r)
        return out

    @property
    def last(self) -> Any:
        return self.requests[-1]

    def body(self, req: Any = None) -> Any:
        r = req if req is not None else self.last
        return json.loads(r.data.decode("utf-8"))

    def query(self, req: Any = None) -> dict[str, str]:
        r = req if req is not None else self.last
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.get_full_url()).query))


def install(monkeypatch: Any) -> Router:
    import meshbook.client as mc

    router = Router()
    monkeypatch.setattr(mc, "_urlopen", router)
    sleeps: list[float] = []
    monkeypatch.setattr(mc, "_sleep", sleeps.append)
    router.sleeps = sleeps  # type: ignore[attr-defined]
    return router


def meshes_payload(*rows: tuple[str, str]) -> dict[str, Any]:
    return ok_list([{"id": i, "name": n, "meshType": "family", "memberRole": "member"}
                    for i, n in rows])
