#!/usr/bin/env python3
"""
Sync project states to match a canonical source project.

Pulls the state list from a source project (e.g. QF) and applies the same
set to every other project in the workspace:

  - **Add** any state in the source that's missing in the target (by name,
    case-insensitive). Color, sequence, and group are copied from source.
  - **Update** any state present in both that has different color, sequence,
    or group than the source.
  - **Never delete** states the target has but source doesn't. Conservative
    by design — operator manages removals manually if desired.

Default is dry-run. Pass --apply to actually mutate.

Env vars required:
  PLANE_BASE_URL   e.g. https://plane.example.com
  PLANE_API_KEY    a workspace PAT with write access on states

Examples:
  # Preview what would change, all projects, source=QF
  PLANE_BASE_URL=https://plane.example.com PLANE_API_KEY=plane_api_... \\
    sync-project-states.py --workspace my-workspace --source QF

  # Apply
  sync-project-states.py --workspace my-workspace --source QF --apply

  # Limit to specific target projects
  sync-project-states.py --workspace my-workspace --source QF --targets PT,WEB
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Iterable

import httpx


STATE_FIELDS = ("name", "color", "sequence", "group")


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _api(client: httpx.Client, base: str, path: str) -> dict | list:
    r = client.get(f"{base}{path}")
    if r.status_code >= 400:
        die(f"GET {path} -> {r.status_code}: {r.text[:200]}")
    return r.json()


def _collect_paginated(client: httpx.Client, base: str, path: str) -> list[dict]:
    """Plane returns either a bare list or a paginated envelope. Handle both."""
    result: list[dict] = []
    next_path: str | None = path
    while next_path:
        data = _api(client, base, next_path)
        if isinstance(data, list):
            result.extend(data)
            next_path = None
        elif isinstance(data, dict):
            # Paginated shape: {"results": [...], "next_cursor": ..., "next_page_results": bool}
            result.extend(data.get("results", []))
            if data.get("next_page_results") and data.get("next_cursor"):
                # Cursor-based pagination — preserve original path, append cursor
                sep = "&" if "?" in path else "?"
                next_path = f"{path}{sep}cursor={data['next_cursor']}"
            else:
                next_path = None
        else:
            die(f"unexpected response shape at {next_path}: {type(data).__name__}")
    return result


def list_projects(client: httpx.Client, base: str, workspace: str) -> list[dict]:
    return _collect_paginated(
        client, base, f"/api/v1/workspaces/{workspace}/projects/"
    )


def list_states(
    client: httpx.Client, base: str, workspace: str, project_id: str
) -> list[dict]:
    return _collect_paginated(
        client,
        base,
        f"/api/v1/workspaces/{workspace}/projects/{project_id}/states/",
    )


def create_state(
    client: httpx.Client,
    base: str,
    workspace: str,
    project_id: str,
    state: dict,
) -> dict:
    payload = {k: state[k] for k in STATE_FIELDS}
    r = client.post(
        f"{base}/api/v1/workspaces/{workspace}/projects/{project_id}/states/",
        json=payload,
    )
    if r.status_code >= 400:
        die(
            f"POST state {payload['name']!r} -> {r.status_code}: {r.text[:200]}"
        )
    return r.json()


def update_state(
    client: httpx.Client,
    base: str,
    workspace: str,
    project_id: str,
    state_id: str,
    fields: dict,
) -> dict:
    r = client.patch(
        f"{base}/api/v1/workspaces/{workspace}/projects/{project_id}/states/{state_id}/",
        json=fields,
    )
    if r.status_code >= 400:
        die(f"PATCH state {state_id} -> {r.status_code}: {r.text[:200]}")
    return r.json()


def _norm(s: str) -> str:
    return s.strip().lower()


def diff(
    source: list[dict], target: list[dict]
) -> tuple[list[dict], list[tuple[dict, dict, dict]]]:
    """Return (to_create, to_update) where to_update is list of
    (source_state, target_state, fields_to_patch)."""
    src_by_name = {_norm(s["name"]): s for s in source}
    tgt_by_name = {_norm(s["name"]): s for s in target}

    to_create: list[dict] = []
    to_update: list[tuple[dict, dict, dict]] = []

    for name, src in src_by_name.items():
        if name not in tgt_by_name:
            to_create.append(src)
            continue
        tgt = tgt_by_name[name]
        delta = {}
        for field in ("color", "sequence", "group"):
            if str(src.get(field)) != str(tgt.get(field)):
                delta[field] = src.get(field)
        if delta:
            to_update.append((src, tgt, delta))

    return to_create, to_update


def fmt_states(states: Iterable[dict]) -> str:
    return ", ".join(
        f"{s['name']}({s.get('color','?')},{s.get('group','?')})" for s in states
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Sync project states to match a canonical source project."
    )
    p.add_argument("--workspace", required=True, help="Workspace slug")
    p.add_argument(
        "--source",
        required=True,
        help="Source project identifier (e.g. QF) or UUID",
    )
    p.add_argument(
        "--targets",
        default=None,
        help="Comma-separated target identifiers/UUIDs. Default: all projects except source.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually mutate (default is dry-run).",
    )
    args = p.parse_args(argv)

    base = os.environ.get("PLANE_BASE_URL")
    pat = os.environ.get("PLANE_API_KEY")
    if not base:
        die("PLANE_BASE_URL env var is required")
    if not pat:
        die("PLANE_API_KEY env var is required")
    base = base.rstrip("/")

    client = httpx.Client(
        headers={"X-API-Key": pat, "Content-Type": "application/json"},
        timeout=30.0,
    )

    projects = list_projects(client, base, args.workspace)
    if not projects:
        die("no projects found in workspace (check workspace slug + PAT scope)")

    def _resolve(token: str) -> dict | None:
        token_norm = token.strip().lower()
        for proj in projects:
            if (
                proj.get("identifier", "").lower() == token_norm
                or str(proj.get("id", "")).lower() == token_norm
            ):
                return proj
        return None

    source = _resolve(args.source)
    if source is None:
        die(f"source project {args.source!r} not found in workspace")
    print(f"source: {source['identifier']} ({source['id']}) - {source['name']}")

    if args.targets:
        target_tokens = [t.strip() for t in args.targets.split(",") if t.strip()]
        targets = []
        for tok in target_tokens:
            proj = _resolve(tok)
            if proj is None:
                die(f"target project {tok!r} not found")
            if proj["id"] == source["id"]:
                continue
            targets.append(proj)
    else:
        targets = [p for p in projects if p["id"] != source["id"]]

    if not targets:
        print("no target projects to sync. exiting.")
        return 0

    src_states = list_states(client, base, args.workspace, source["id"])
    print(f"\nsource states ({len(src_states)}): {fmt_states(src_states)}\n")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== {mode}: syncing {len(targets)} target project(s) ===\n")

    created_total = updated_total = 0

    for tgt in targets:
        tgt_states = list_states(client, base, args.workspace, tgt["id"])
        to_create, to_update = diff(src_states, tgt_states)

        if not to_create and not to_update:
            print(f"  [{tgt['identifier']}] {tgt['name']}: in sync ✓")
            continue

        print(
            f"  [{tgt['identifier']}] {tgt['name']}: "
            f"{len(to_create)} to create, {len(to_update)} to update"
        )
        for s in to_create:
            print(
                f"    + add {s['name']!r} (color={s.get('color')}, group={s.get('group')})"
            )
            if args.apply:
                create_state(client, base, args.workspace, tgt["id"], s)
                created_total += 1
        for src, tgt_state, delta in to_update:
            changes = ", ".join(f"{k}: {tgt_state.get(k)!r} → {v!r}" for k, v in delta.items())
            print(f"    ~ patch {src['name']!r}: {changes}")
            if args.apply:
                update_state(
                    client,
                    base,
                    args.workspace,
                    tgt["id"],
                    tgt_state["id"],
                    delta,
                )
                updated_total += 1
        print()

    if args.apply:
        print(f"=== done: created {created_total}, updated {updated_total} ===")
    else:
        print("=== dry-run only. re-run with --apply to mutate ===")

    return 0


if __name__ == "__main__":
    sys.exit(main())
