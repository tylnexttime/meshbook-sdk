#!/usr/bin/env python3
"""refresh_openapi — re-pin tests/contract/openapi.json (stdlib only).

Source, in order of preference:
  --from FILE   a local spec, normally the server repo's docs/openapi.json
                (it carries routes prod does not have yet);
  --url URL     a public spec URL (default https://meshbook.org/api/openapi.json;
                unauthenticated — no token is ever sent).

Writes the spec verbatim (bytes unchanged) and ``openapi.source.json``
beside it: source, retrieval time, sha256, path/operation counts — the
same provenance file @meshbook/sdk keeps, so the two snapshots can be
compared at a glance.

  python scripts/refresh_openapi.py --from E:/dev/meshbook/docs/openapi.json
  python scripts/refresh_openapi.py            # fetch prod
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://meshbook.org/api/openapi.json"
OUT = Path(__file__).resolve().parent.parent / "tests" / "contract" / "openapi.json"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="refresh_openapi")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--from", dest="from_file", help="local OpenAPI JSON file")
    src.add_argument("--url", default=None, help=f"spec URL (default {DEFAULT_URL})")
    ap.add_argument("--out", default=str(OUT), help="where to write the snapshot")
    ap.add_argument("--note", default=None, help="free-text provenance note")
    args = ap.parse_args(argv)

    if args.from_file:
        raw = Path(args.from_file).read_bytes()
        source = str(Path(args.from_file).resolve())
    else:
        url = args.url or DEFAULT_URL
        req = urllib.request.Request(url, headers={"User-Agent": "meshbook-sdk-refresh-openapi",
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read()
        source = url
    spec = json.loads(raw.decode("utf-8"))
    paths = spec.get("paths") or {}
    ops = sum(1 for item in paths.values() for m in item if m in METHODS)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    meta = {
        "source": source,
        "retrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "openapi": spec.get("openapi"),
        "infoVersion": (spec.get("info") or {}).get("version"),
        "paths": len(paths),
        "operations": ops,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if args.note:
        meta["note"] = args.note
    out.with_name("openapi.source.json").write_text(json.dumps(meta, indent=2) + "\n",
                                                     encoding="utf-8")
    print(f"wrote {out} ({len(paths)} paths, {ops} operations, sha256 {meta['sha256'][:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
