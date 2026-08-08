#!/usr/bin/env python
"""Safely add/remove TE-Speed wiring in MiniMax H3 ComfyUI workflows.

Unlike the legacy backup-restore approach, uninstall is surgical:
- only nodes created by this installer are removed;
- unrelated user edits made after installation are preserved;
- if the TE-Speed wiring itself was changed, rollback stops for that workflow
  instead of guessing and overwriting the file.

Both top-level workflows and nested subgraphs are supported.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TE_NODE_TYPE = "TESpeedMiniMaxH3"
MARKER_KEY = "tespeed_safe_installer"
MARKER_VALUE = "v2"
RESTORE_KEY = "tespeed_restore"
TE_WIDGETS = [0.12, 0.1, 0.9, 2, "cpu"]

TE_TEMPLATE = {
    "id": 0,
    "type": TE_NODE_TYPE,
    "pos": [0, 0],
    "size": [306.640625, 154],
    "flags": {},
    "order": 19,
    "mode": 0,
    "inputs": [
        {"localized_name": "model", "name": "model", "type": "MODEL", "link": None},
        {"localized_name": "processing_control_value", "name": "processing_control_value", "type": "FLOAT", "widget": {"name": "processing_control_value"}, "link": None},
        {"localized_name": "processing_percent_1", "name": "processing_percent_1", "type": "FLOAT", "widget": {"name": "processing_percent_1"}, "link": None},
        {"localized_name": "processing_percent_2", "name": "processing_percent_2", "type": "FLOAT", "widget": {"name": "processing_percent_2"}, "link": None},
        {"localized_name": "mcs", "name": "mcs", "type": "INT", "widget": {"name": "mcs"}, "link": None},
        {"localized_name": "device", "name": "device", "type": "COMBO", "widget": {"name": "device"}, "link": None},
    ],
    "outputs": [{"localized_name": "模型", "name": "MODEL", "type": "MODEL", "links": []}],
    "properties": {"Node name for S&R": TE_NODE_TYPE, MARKER_KEY: MARKER_VALUE},
    "widgets_values": list(TE_WIDGETS),
}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iter_json_files(paths):
    seen = set()
    for raw in paths:
        p = Path(raw)
        items = sorted(p.glob("*.json")) if p.is_dir() else [p]
        for item in items:
            if item.is_file() and item.suffix.lower() == ".json" and item not in seen:
                seen.add(item)
                yield item


def read_workflow(path: Path):
    data = path.read_bytes()
    bom = data.startswith(b"\xef\xbb\xbf")
    return json.loads(data.decode("utf-8-sig")), bom, data


def write_workflow(path: Path, wf, bom: bool):
    text = json.dumps(wf, ensure_ascii=False, indent=2)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


def recovery_backup(path: Path, original: bytes):
    recovery = path.parent / ".tespeed_recovery"
    recovery.mkdir(exist_ok=True)
    dest = recovery / f"{path.name}.{utc_stamp()}.{sha256(original)[:12]}.bak"
    if not dest.exists():
        dest.write_bytes(original)
    return dest


def container_label(container, root=False):
    if root:
        return "top-level"
    return str(container.get("name") or container.get("id") or "subgraph")


def iter_containers(wf):
    # top-level first
    if isinstance(wf, dict) and isinstance(wf.get("nodes"), list) and isinstance(wf.get("links"), list):
        yield wf, True
    defs = wf.get("definitions") if isinstance(wf, dict) else None
    subs = (defs or {}).get("subgraphs") if isinstance(defs, dict) else None
    for sg in subs or []:
        if isinstance(sg, dict) and isinstance(sg.get("nodes"), list) and isinstance(sg.get("links"), list):
            yield sg, False


def is_h3_container(container) -> bool:
    types = {n.get("type") for n in container.get("nodes", []) if isinstance(n, dict)}
    return (
        {"UNETLoader", "BasicScheduler", "BasicGuider"} <= types
        and any(isinstance(t, str) and t.startswith("MiniMaxH3") for t in types)
    )


def maps(container):
    nodes = {n.get("id"): n for n in container.get("nodes", []) if isinstance(n, dict) and "id" in n}
    links = {l.get("id"): l for l in container.get("links", []) if isinstance(l, dict) and "id" in l}
    return nodes, links


def owned_te_nodes(container):
    return [
        n for n in container.get("nodes", [])
        if n.get("type") == TE_NODE_TYPE and (n.get("properties") or {}).get(MARKER_KEY) == MARKER_VALUE
    ]


def foreign_te_nodes(container):
    return [
        n for n in container.get("nodes", [])
        if n.get("type") == TE_NODE_TYPE and (n.get("properties") or {}).get(MARKER_KEY) != MARKER_VALUE
    ]


def find_direct_chain(container):
    nodes, links = maps(container)
    unets = [n for n in nodes.values() if n.get("type") == "UNETLoader"]
    scheds = [n for n in nodes.values() if n.get("type") == "BasicScheduler"]
    guiders = [n for n in nodes.values() if n.get("type") == "BasicGuider"]
    candidates = []
    for unet in unets:
        for sched in scheds:
            ls = next((l for l in links.values()
                       if l.get("origin_id") == unet["id"] and l.get("origin_slot") == 0
                       and l.get("target_id") == sched["id"] and l.get("target_slot") == 0), None)
            if ls is None:
                continue
            for guider in guiders:
                lg = next((l for l in links.values()
                           if l.get("origin_id") == unet["id"] and l.get("origin_slot") == 0
                           and l.get("target_id") == guider["id"] and l.get("target_slot") == 0), None)
                if lg is not None:
                    candidates.append((unet, sched, guider, ls, lg))
    return candidates


def next_ids(container):
    nodes, links = maps(container)
    state = container.get("state") if isinstance(container.get("state"), dict) else {}
    node_id = max([state.get("lastNodeId", 0)] + [int(i) for i in nodes if isinstance(i, int)]) + 1
    link_id = max([state.get("lastLinkId", 0)] + [int(i) for i in links if isinstance(i, int)]) + 1
    return node_id, link_id


def patch_container(container):
    """Return (status, message). status: patched/already/skip/conflict."""
    if owned_te_nodes(container):
        return "already", "safe TE-Speed node already present"
    if foreign_te_nodes(container):
        return "skip", "existing TE-Speed node is not owned by this installer"
    candidates = find_direct_chain(container)
    if len(candidates) == 0:
        return "skip", "expected UNETLoader -> Scheduler/Guider direct wiring not found"
    if len(candidates) > 1:
        return "conflict", "multiple candidate sampling chains found; refusing to guess"

    unet, sched, guider, l_sched, l_guider = candidates[0]
    new_id, new_link = next_ids(container)
    te = copy.deepcopy(TE_TEMPLATE)
    te["id"] = new_id
    unet_pos = unet.get("pos") or [0, 0]
    te["pos"] = [unet_pos[0] + 680, unet_pos[1]]
    te["inputs"][0]["link"] = l_sched["id"]
    te["outputs"][0]["links"] = [l_guider["id"], new_link]
    te["properties"][RESTORE_KEY] = {
        "unet_id": unet["id"],
        "scheduler_id": sched["id"],
        "guider_id": guider["id"],
        "unet_to_te_link_id": l_sched["id"],
        "te_to_guider_link_id": l_guider["id"],
        "te_to_scheduler_link_id": new_link,
    }
    container["nodes"].append(te)

    # Re-purpose the two original links, then add one new link.
    l_sched["target_id"] = new_id
    l_sched["target_slot"] = 0
    l_guider["origin_id"] = new_id
    l_guider["origin_slot"] = 0
    container["links"].append({
        "id": new_link, "origin_id": new_id, "origin_slot": 0,
        "target_id": sched["id"], "target_slot": 0, "type": "MODEL",
    })

    # Keep any unrelated links a user may already have.
    outputs = unet.get("outputs") or []
    if outputs:
        current = list(outputs[0].get("links") or [])
        outputs[0]["links"] = [x for x in current if x != l_guider["id"]]
    inputs = sched.get("inputs") or []
    if inputs:
        inputs[0]["link"] = new_link

    if isinstance(container.get("state"), dict):
        container["state"]["lastNodeId"] = max(container["state"].get("lastNodeId", 0), new_id)
        container["state"]["lastLinkId"] = max(container["state"].get("lastLinkId", 0), new_link)
    return "patched", f"inserted TE-Speed node {new_id}"


def revert_one_node(container, te):
    nodes, links = maps(container)
    meta = (te.get("properties") or {}).get(RESTORE_KEY)
    if not isinstance(meta, dict):
        return False, "owned TE-Speed node is missing restore metadata"
    try:
        unet = nodes[meta["unet_id"]]
        sched = nodes[meta["scheduler_id"]]
        guider = nodes[meta["guider_id"]]
        l_in = links[meta["unet_to_te_link_id"]]
        l_guider = links[meta["te_to_guider_link_id"]]
        l_sched = links[meta["te_to_scheduler_link_id"]]
    except KeyError as exc:
        return False, f"required node/link no longer exists: {exc}"

    te_id = te["id"]
    te_output_links = set(((te.get("outputs") or [{}])[0].get("links") or []))
    expected = [
        l_in.get("origin_id") == unet["id"] and l_in.get("origin_slot") == 0 and l_in.get("target_id") == te_id and l_in.get("target_slot") == 0,
        l_guider.get("origin_id") == te_id and l_guider.get("origin_slot") == 0 and l_guider.get("target_id") == guider["id"] and l_guider.get("target_slot") == 0,
        l_sched.get("origin_id") == te_id and l_sched.get("origin_slot") == 0 and l_sched.get("target_id") == sched["id"] and l_sched.get("target_slot") == 0,
        (te.get("inputs") or [{}])[0].get("link") == l_in["id"],
        (sched.get("inputs") or [{}])[0].get("link") == l_sched["id"],
        (guider.get("inputs") or [{}])[0].get("link") == l_guider["id"],
        te_output_links == {l_guider["id"], l_sched["id"]},
    ]
    if not all(expected):
        return False, "TE-Speed wiring was changed after install; refusing to guess"

    # Restore only the edges we changed.
    l_in["target_id"] = sched["id"]
    l_in["target_slot"] = 0
    l_guider["origin_id"] = unet["id"]
    l_guider["origin_slot"] = 0
    container["links"] = [l for l in container.get("links", []) if l.get("id") != l_sched["id"]]

    outputs = unet.get("outputs") or []
    if outputs:
        current = list(outputs[0].get("links") or [])
        if l_in["id"] not in current:
            current.append(l_in["id"])
        if l_guider["id"] not in current:
            current.append(l_guider["id"])
        outputs[0]["links"] = current
    (sched.get("inputs") or [{}])[0]["link"] = l_in["id"]
    # Guider input link ID never changed; only the link origin changed.

    container["nodes"] = [n for n in container.get("nodes", []) if n.get("id") != te_id]
    return True, f"removed TE-Speed node {te_id} and restored direct model wiring"


def cmd_add(files):
    conflicts = 0
    changed_files = 0
    for path in files:
        try:
            wf, bom, original = read_workflow(path)
        except Exception as exc:
            print(f"[SKIP] {path.name}: unreadable workflow JSON ({exc})")
            continue
        changed = False
        relevant = False
        for container, is_root in iter_containers(wf):
            if not is_h3_container(container):
                continue
            relevant = True
            status, msg = patch_container(container)
            print(f"[{status.upper():8}] {path.name} / {container_label(container, is_root)}: {msg}")
            if status == "patched":
                changed = True
            elif status == "conflict":
                conflicts += 1
        if changed:
            backup = recovery_backup(path, original)
            write_workflow(path, wf, bom)
            changed_files += 1
            print(f"[WRITE] {path.name}: updated safely; recovery copy: {backup.name}")
        elif not relevant:
            print(f"[SKIP] {path.name}: no MiniMax H3 sampling container found")
    print(f"TE-Speed workflow add summary: changed_files={changed_files}, conflicts={conflicts}")
    return 2 if conflicts else 0


def cmd_revert(files):
    conflicts = 0
    changed_files = 0
    for path in files:
        try:
            wf, bom, original = read_workflow(path)
        except Exception as exc:
            print(f"[SKIP] {path.name}: unreadable workflow JSON ({exc})")
            continue
        changed = False
        file_conflicts = 0
        for container, is_root in iter_containers(wf):
            foreign = list(foreign_te_nodes(container))
            if foreign:
                file_conflicts += len(foreign)
                conflicts += len(foreign)
                print(f"[CONFLICT] {path.name} / {container_label(container, is_root)}: "
                      f"found {len(foreign)} TE-Speed node(s) not owned by this installer; refusing plugin removal")
            owned = list(owned_te_nodes(container))
            for te in owned:
                ok, msg = revert_one_node(container, te)
                label = container_label(container, is_root)
                if ok:
                    changed = True
                    print(f"[REVERT] {path.name} / {label}: {msg}")
                else:
                    conflicts += 1
                    file_conflicts += 1
                    print(f"[CONFLICT] {path.name} / {label}: {msg}")
        if changed and file_conflicts == 0:
            backup = recovery_backup(path, original)
            write_workflow(path, wf, bom)
            changed_files += 1
            print(f"[WRITE] {path.name}: surgical rollback saved; recovery copy: {backup.name}")
        elif changed:
            # Do not write a partly-reverted file when any conflict exists in this run.
            print(f"[HOLD] {path.name}: not written because a rollback conflict was detected")
    print(f"TE-Speed workflow revert summary: changed_files={changed_files}, conflicts={conflicts}")
    return 2 if conflicts else 0


def cmd_check(files):
    owned = foreign = 0
    for path in files:
        try:
            wf, _, _ = read_workflow(path)
        except Exception:
            continue
        for container, is_root in iter_containers(wf):
            o = len(owned_te_nodes(container))
            f = len(foreign_te_nodes(container))
            if o or f:
                print(f"[CHECK] {path.name} / {container_label(container, is_root)}: owned={o}, other={f}")
            owned += o
            foreign += f
    print(f"TE-Speed workflow check: owned={owned}, other={foreign}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Safe TE-Speed MiniMax H3 workflow wiring manager")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", action="store_true")
    group.add_argument("--revert", action="store_true")
    group.add_argument("--check", action="store_true")
    parser.add_argument("paths", nargs="+", help="workflow .json files and/or directories")
    args = parser.parse_args()
    files = list(iter_json_files(args.paths))
    if not files:
        print("TE-Speed: no workflow JSON files found; nothing to do")
        raise SystemExit(0)
    code = cmd_add(files) if args.add else cmd_revert(files) if args.revert else cmd_check(files)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
