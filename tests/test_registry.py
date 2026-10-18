"""The endpoint registry is the ONLY door to the network.

The contract check reads ``meshbook._endpoints`` instead of guessing at call
sites, which is only sound if nothing bypasses it. Three guards:

* no module under ``meshbook/`` calls the raw ``request()`` / ``_send()``
  except the client core itself (AST, so a new bypass fails here);
* every registry entry is used by some code (no dead rows inflating the
  contract or the coverage figure);
* (conftest) every request the suite sends matches a registry entry.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from meshbook._endpoints import ENDPOINTS, Endpoint, calls, pending

PKG = Path(__file__).resolve().parents[1] / "meshbook"
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
_PATHLIKE = re.compile(r"^/api/[\w\-/{}.]*$")  # a path, not a sentence that mentions one


def _sources():
    return {p: p.read_text(encoding="utf-8") for p in PKG.rglob("*.py")}


def test_entries_are_well_formed():
    for key, ep in ENDPOINTS.items():
        assert ep.method in METHODS, key
        assert ep.path.startswith("/api/"), key
        assert ep.mesh in ("active", "path", "none"), key
        if ep.mesh == "path":
            assert "{mesh_id}" in ep.path, key
    assert len(calls()) == len({(e.method, e.path) for e in ENDPOINTS.values()})
    assert isinstance(pending(), dict)


def test_render_percent_encodes_and_requires_params():
    ep = Endpoint("DELETE", "/api/chat-messages/{message_id}/reactions/{emoji}")
    assert ep.render({"message_id": "m 1", "emoji": "✅"}) == \
        "/api/chat-messages/m%201/reactions/%E2%9C%85"
    with pytest.raises(KeyError):
        ep.render()


def test_no_module_bypasses_the_registry():
    offenders = []
    for path, text in _sources().items():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("request", "_send"):
                continue
            # The client core implements request()/_call()/_list()/_download()
            # on top of _send(); nothing else may call either.
            if path.name == "client.py" and node.func.attr == "_send":
                continue
            offenders.append(f"{path.relative_to(PKG)}:{node.lineno} .{node.func.attr}(")
    assert offenders == []


def test_no_hand_written_api_paths_outside_the_registry():
    """A literal '/api/...' in a call outside _endpoints.py is a bypass in the making."""
    offenders = []
    for path, text in _sources().items():
        if path.name == "_endpoints.py":
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call):
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and _PATHLIKE.match(arg.value):
                        offenders.append(f"{path.relative_to(PKG)}:{node.lineno}")
    assert offenders == []


def test_every_registry_entry_is_used():
    code = "\n".join(t for p, t in _sources().items() if p.name != "_endpoints.py")
    unused = [k for k in ENDPOINTS if f'"{k}"' not in code]
    assert unused == []
