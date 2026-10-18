"""F3, the SDK half: every (method, path) the SDK calls exists in the
pinned OpenAPI snapshot (tests/contract/openapi.json, refreshed with
scripts/refresh_openapi.py).

The checker itself is exercised against synthetic specs too, so each of its
failure modes is shown to go red (a check that cannot fail proves nothing).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests" / "contract" / "openapi.json"


def _load_checker():
    spec = importlib.util.spec_from_file_location("contract_check", ROOT / "scripts" / "contract_check.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["contract_check"] = mod
    spec.loader.exec_module(mod)
    return mod


cc = _load_checker()


def _spec(tmp_path, *ops):
    paths: dict = {}
    for method, path in ops:
        paths.setdefault(path, {})[method.lower()] = {"responses": {}}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps({"openapi": "3.1.0", "paths": paths}), encoding="utf-8")
    return p


def _calls(tmp_path, *entries):
    p = tmp_path / "calls.json"
    p.write_text(json.dumps(list(entries)), encoding="utf-8")
    return p


# ─── the real gate ─────────────────────────────────────────────────────


def test_every_sdk_call_is_in_the_pinned_spec(capsys):
    """The CI gate. Strict: no --allow-pending, nothing missing, no stale marks."""
    code = cc.main(["--spec", str(SNAPSHOT), "--registry", "meshbook._endpoints"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "missing: 0" in out and "OK" in out


def test_the_snapshot_has_provenance():
    meta = json.loads((SNAPSHOT.parent / "openapi.source.json").read_text(encoding="utf-8"))
    import hashlib

    assert meta["sha256"] == hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()
    assert meta["operations"] > 200


def test_json_output_reports_coverage(capsys):
    cc.main(["--spec", str(SNAPSHOT), "--registry", "meshbook._endpoints", "--json"])
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is True
    assert res["calls"] >= 90 and res["covered_operations"] >= 90
    assert 0 < res["coverage"] < 1


# ─── the checker can fail ──────────────────────────────────────────────


def test_a_call_the_spec_lacks_exits_1(tmp_path, capsys):
    spec = _spec(tmp_path, ("GET", "/api/a"))
    calls = _calls(tmp_path, ["GET", "/api/a"], ["POST", "/api/b/{id}"])
    assert cc.main(["--spec", str(spec), "--calls", str(calls)]) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out and "/api/b/{id}" in out and "FAIL" in out


def test_method_matters(tmp_path):
    spec = _spec(tmp_path, ("GET", "/api/a"))
    assert cc.main(["--spec", str(spec), "--calls", str(_calls(tmp_path, ["DELETE", "/api/a"]))]) == 1


def test_pending_fails_strict_and_passes_with_allow_pending(tmp_path, capsys):
    spec = _spec(tmp_path, ("GET", "/api/a"))
    calls = _calls(tmp_path, {"method": "PUT", "path": "/api/a/{x}", "pending": "server packet"})
    assert cc.main(["--spec", str(spec), "--calls", str(calls)]) == 1
    assert cc.main(["--spec", str(spec), "--calls", str(calls), "--allow-pending"]) == 0
    assert "server packet" in capsys.readouterr().out


def test_a_stale_pending_mark_fails_even_with_allow_pending(tmp_path, capsys):
    spec = _spec(tmp_path, ("PUT", "/api/a/{event_id}"))
    calls = _calls(tmp_path, {"method": "PUT", "path": "/api/a/{id}", "pending": "old reason"})
    assert cc.main(["--spec", str(spec), "--calls", str(calls), "--allow-pending"]) == 1
    assert "STALE" in capsys.readouterr().out


def test_parameter_names_do_not_matter_but_positions_do(tmp_path):
    spec = _spec(tmp_path, ("GET", "/api/x/{event_id}/y"))
    ok = _calls(tmp_path, ["GET", "/api/x/{id}/y"])
    assert cc.main(["--spec", str(spec), "--calls", str(ok)]) == 0
    bad = _calls(tmp_path, ["GET", "/api/x/y/{id}"])
    assert cc.main(["--spec", str(spec), "--calls", str(bad)]) == 1


def test_a_literal_segment_fills_a_spec_parameter_but_not_the_reverse(tmp_path):
    spec = _spec(tmp_path, ("GET", "/api/entities/{entity_type}/{entity_id}/chat"),
                 ("POST", "/api/meshes/active"))
    assert cc.main(["--spec", str(spec), "--calls",
                    str(_calls(tmp_path, ["GET", "/api/entities/mesh/{id}/chat"]))]) == 0
    assert cc.main(["--spec", str(spec), "--calls",
                    str(_calls(tmp_path, ["POST", "/api/meshes/{mesh_id}"]))]) == 1


def test_ast_scan_finds_hand_built_calls(tmp_path, capsys):
    """The mode meshbook-cli / meshbook-mcp can run on their sources today."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text(
        'def go(mid, cfg):\n'
        '    _api_call("GET", f"/api/meshes/{mid}/channels", cfg=cfg)\n'
        '    client.request("POST", "/api/nowhere")\n'
        '    log("GET is a word", "/api/not-a-call-shape-without-upper")\n'
        '    """GET /api/in-a-docstring"""\n',
        encoding="utf-8",
    )
    spec = _spec(tmp_path, ("GET", "/api/meshes/{mesh_id}/channels"))
    assert cc.main(["--spec", str(spec), "--scan", str(src)]) == 1
    out = capsys.readouterr().out
    assert "/api/nowhere" in out and "mod.py:3" in out
    assert "not-a-call" not in out and "in-a-docstring" not in out
    assert "in spec: 1" in out


def test_usage_errors_exit_2(tmp_path):
    assert cc.main(["--spec", str(tmp_path / "missing.json"), "--calls",
                    str(_calls(tmp_path, ["GET", "/api/a"]))]) == 2
    with pytest.raises(SystemExit) as ei:
        cc.main(["--spec", "x.json"])
    assert ei.value.code == 2
