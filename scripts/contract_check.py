#!/usr/bin/env python3
"""contract_check — every (method, path) a meshbook client calls must exist
in the server's OpenAPI spec (PLAN 2026-10-19, F3).

Stdlib only, one file, no import of the package being checked unless you
ask for registry mode — so meshbook-cli and meshbook-mcp can VENDOR this
file (copy it to their own ``scripts/``) or run it from an installed SDK.

Where the calls come from (combine freely; the union is checked):

  --registry MODULE[:ATTR]   a Python endpoint registry. ATTR defaults to
                             trying ``calls()``/``pending()`` functions, then
                             an ``ENDPOINTS`` mapping of objects with
                             ``.method``/``.path``/``.pending``. The SDK's is
                             ``meshbook._endpoints`` — authoritative, because
                             every SDK request goes through it.
  --scan PATH                AST scan of .py files under PATH for calls of
                             the shape ``f("GET", "/api/...")`` (string or
                             f-string path; ``{expr}`` becomes a parameter).
                             For code that still builds requests by hand —
                             the CLI and MCP today — and to prove, after the
                             port, that none are left.
  --calls FILE               a JSON list of [method, path] (or
                             {"method","path","pending"}) entries.

Exit status: 0 = every call is in the spec; 1 = a call the spec lacks, or a
STALE pending mark (the spec has the route now: remove the mark); 2 = usage
error. Calls marked pending (route in today's contract, not yet in the
published spec) fail too unless ``--allow-pending``; either way they are
listed. Path parameters are compared by position, not name
(``/x/{id}`` == ``/x/{event_id}``).

Examples:
  python scripts/contract_check.py --spec tests/contract/openapi.json \
      --registry meshbook._endpoints --allow-pending
  python scripts/contract_check.py --spec tests/contract/openapi.json --scan mesh/
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_PARAM = re.compile(r"\{[^{}]*\}")


def normalise(path: str) -> str:
    """Path template → comparable key: params collapsed, query/trailing '/' dropped."""
    path = path.split("?", 1)[0]
    if len(path) > 1:
        path = path.rstrip("/")
    return _PARAM.sub("{}", path)


# ─── the spec ──────────────────────────────────────────────────────────


def spec_operations(spec: dict[str, Any], prefix: str) -> dict[tuple[str, str], str]:
    """(METHOD, normalised path) → original path, for every operation under *prefix*."""
    ops: dict[tuple[str, str], str] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not path.startswith(prefix) or not isinstance(item, dict):
            continue
        for method in item:
            if method.upper() in HTTP_METHODS:
                ops[(method.upper(), normalise(path))] = path
    return ops


# ─── call sources ──────────────────────────────────────────────────────

Call = tuple[str, str, "str | None", str]  # method, path, pending reason, origin


def from_registry(spec: str) -> list[Call]:
    module_name, _, attr = spec.partition(":")
    module = importlib.import_module(module_name)
    origin = f"registry {spec}"
    if attr:
        obj = getattr(module, attr)
        return list(_entries(obj() if callable(obj) else obj, origin))
    if callable(getattr(module, "calls", None)):
        pend = module.pending() if callable(getattr(module, "pending", None)) else {}
        return [(m.upper(), p, pend.get((m, p)), origin) for m, p in module.calls()]
    if hasattr(module, "ENDPOINTS"):
        return list(_entries(module.ENDPOINTS, origin))
    raise SystemExit(f"contract_check: {module_name} has no calls()/ENDPOINTS; pass MODULE:ATTR")


def _entries(obj: Any, origin: str) -> Iterable[Call]:
    values = obj.values() if isinstance(obj, dict) else obj
    for e in values:
        if isinstance(e, (list, tuple)):
            method, path = e[0], e[1]
            pending = e[3] if len(e) > 3 else None
        elif isinstance(e, dict):
            method, path, pending = e["method"], e["path"], e.get("pending")
        else:
            method, path, pending = e.method, e.path, getattr(e, "pending", None)
        yield (str(method).upper(), str(path), pending or None, origin)


def from_json(file: str) -> list[Call]:
    data = json.loads(Path(file).read_text(encoding="utf-8"))
    return list(_entries(data, f"json {file}"))


def _render(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
            else:
                out.append("{}")
        return "".join(out)
    return None


def from_scan(root: str, prefix: str) -> list[Call]:
    """Calls shaped like f("METHOD", "/api/...") anywhere under *root*."""
    found: list[Call] = []
    base = Path(root)
    files = [base] if base.is_file() else sorted(base.rglob("*.py"))
    for file in files:
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except (SyntaxError, UnicodeDecodeError) as e:
            raise SystemExit(f"contract_check: cannot parse {file}: {e}") from e
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            args = list(node.args) + [kw.value for kw in node.keywords]
            method = next(
                (a.value for a in args if isinstance(a, ast.Constant)
                 and isinstance(a.value, str) and a.value.upper() in HTTP_METHODS
                 and a.value.isupper()),
                None,
            )
            if method is None:
                continue
            for a in args:
                path = _render(a)
                if path and path.startswith(prefix):
                    found.append((method, path, None, f"{file}:{node.lineno}"))
                    break
    return found


# ─── the check ─────────────────────────────────────────────────────────


def _segments_match(client: str, spec: str) -> bool:
    """A literal client segment may fill a spec parameter
    (``/api/entities/mesh/{}/chat`` IS ``/api/entities/{}/{}/chat``); a
    client parameter never stands in for a spec literal."""
    cs, ss = client.split("/"), spec.split("/")
    if len(cs) != len(ss):
        return False
    return all(c == s or s == "{}" for c, s in zip(cs, ss, strict=True))


def _match(key: tuple[str, str], ops: dict[tuple[str, str], str]) -> tuple[str, str] | None:
    """The spec operation a client call hits (exact first), or None."""
    if key in ops:
        return key
    method, path = key
    for op in ops:
        if op[0] == method and _segments_match(path, op[1]):
            return op
    return None


def check(calls: list[Call], ops: dict[tuple[str, str], str], allow_pending: bool) -> dict[str, Any]:
    distinct: dict[tuple[str, str], Call] = {}
    for c in calls:
        key = (c[0], normalise(c[1]))
        prev = distinct.get(key)
        # A pending mark anywhere wins over an unmarked duplicate.
        if prev is None or (c[2] and not prev[2]):
            distinct[key] = c
    present, missing, pending, stale = [], [], [], []
    hit: set[tuple[str, str]] = set()
    for key, c in sorted(distinct.items()):
        op = _match(key, ops)
        in_spec = op is not None
        if op is not None:
            hit.add(op)
        if in_spec and c[2]:
            stale.append(c)
        elif in_spec:
            present.append(c)
        elif c[2]:
            pending.append(c)
        else:
            missing.append(c)
    covered = len(hit)
    ok = not missing and not stale and (allow_pending or not pending)
    return {
        "ok": ok,
        "calls": len(distinct),
        "present": present,
        "missing": missing,
        "pending": pending,
        "stale_pending": stale,
        "spec_operations": len(ops),
        "covered_operations": covered,
        "coverage": covered / len(ops) if ops else 0.0,
    }


def _fmt(c: Call) -> str:
    reason = f"  [pending: {c[2]}]" if c[2] else ""
    return f"  {c[0]:<6} {c[1]}{reason}   ({c[3]})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="contract_check", description=__doc__.split("\n\n")[0])
    ap.add_argument("--spec", required=True, help="OpenAPI JSON file")
    ap.add_argument("--registry", action="append", default=[], help="MODULE[:ATTR]")
    ap.add_argument("--scan", action="append", default=[], help="file or directory to AST-scan")
    ap.add_argument("--calls", action="append", default=[], help="JSON list of calls")
    ap.add_argument("--prefix", default="/api", help="only paths under this prefix (default /api)")
    ap.add_argument("--allow-pending", action="store_true", help="pending calls do not fail")
    ap.add_argument("--path", action="append", default=[], help="prepend to sys.path (registry mode)")
    ap.add_argument("--json", action="store_true", help="machine-readable result on stdout")
    args = ap.parse_args(argv)
    if not (args.registry or args.scan or args.calls):
        ap.error("give at least one of --registry, --scan, --calls")
    for p in reversed(args.path):
        sys.path.insert(0, p)
    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"contract_check: cannot read spec {args.spec}: {e}", file=sys.stderr)
        return 2
    calls: list[Call] = []
    for r in args.registry:
        calls += from_registry(r)
    for s in args.scan:
        calls += from_scan(s, args.prefix)
    for j in args.calls:
        calls += from_json(j)
    calls = [c for c in calls if c[1].startswith(args.prefix)]
    res = check(calls, spec_operations(spec, args.prefix), args.allow_pending)

    if args.json:
        out = dict(res)
        for k in ("present", "missing", "pending", "stale_pending"):
            out[k] = [{"method": c[0], "path": c[1], "pending": c[2], "origin": c[3]} for c in res[k]]
        print(json.dumps(out, indent=2))
        return 0 if res["ok"] else 1

    print(f"contract check against {args.spec} ({res['spec_operations']} {args.prefix} operations)")
    print(f"  client calls (distinct method+path): {res['calls']}")
    print(f"  in spec: {len(res['present'])}   pending: {len(res['pending'])}   "
          f"missing: {len(res['missing'])}   stale pending marks: {len(res['stale_pending'])}")
    if res["missing"]:
        print(f"MISSING from the spec ({len(res['missing'])}):")
        for c in res["missing"]:
            print(_fmt(c))
    if res["pending"]:
        verdict = "allowed (--allow-pending)" if args.allow_pending else "NOT allowed in strict mode"
        print(f"PENDING, marked in the registry ({len(res['pending'])}), {verdict}:")
        for c in res["pending"]:
            print(_fmt(c))
    if res["stale_pending"]:
        print(f"STALE pending marks: the spec has these now, remove the mark "
              f"({len(res['stale_pending'])}):")
        for c in res["stale_pending"]:
            print(_fmt(c))
    covered = res["covered_operations"]
    print(f"coverage: client calls reach {covered} of {res['spec_operations']} spec operations "
          f"({res['coverage'] * 100:.1f}%)")
    print("OK" if res["ok"] else "FAIL")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
