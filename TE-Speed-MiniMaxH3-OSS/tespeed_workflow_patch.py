#!/usr/bin/env python
"""Safely add/remove TE-Speed wiring in MiniMax H3 ComfyUI workflows.

V3 goals:
- support top-level workflows and definitions.subgraphs;
- support LoRA / ModelSampling / other MODEL patches by inserting TE-Speed after
  the final common MODEL producer feeding BasicScheduler + BasicGuider;
- never guess across genuinely divergent model branches;
- mark installer-owned nodes and surgically revert only our own wiring;
- preserve unrelated user edits made after installation.
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
MARKER_VALUE = "v3"
RESTORE_KEY = "tespeed_restore"
TE_WIDGETS = [0.12, 0.1, 0.9, 2, "auto"]

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
        {"BasicScheduler", "BasicGuider"} <= types
        and any(isinstance(t, str) and t.startswith("MiniMaxH3") for t in types)
    )


def maps(container):
    nodes = {n.get("id"): n for n in container.get("nodes", []) if isinstance(n, dict) and "id" in n}
    links = {l.get("id"): l for l in container.get("links", []) if isinstance(l, dict) and "id" in l}
    return nodes, links


def owned_te_nodes(container):
    return [
        n for n in container.get("nodes", [])
        if n.get("type") == TE_NODE_TYPE and (n.get("properties") or {}).get(MARKER_KEY) in {"v2", "v3"}
    ]


def foreign_te_nodes(container):
    return [
        n for n in container.get("nodes", [])
        if n.get("type") == TE_NODE_TYPE and (n.get("properties") or {}).get(MARKER_KEY) not in {"v2", "v3"}
    ]


def _model_input_link(node, links):
    """Return the link feeding a node's MODEL/model input when unambiguous."""
    inputs = node.get("inputs") or []
    candidates = []
    for idx, inp in enumerate(inputs):
        name = str(inp.get("name", "")).lower()
        typ = str(inp.get("type", "")).upper()
        if name == "model" or typ == "MODEL":
            lid = inp.get("link")
            if lid in links:
                candidates.append((idx, links[lid]))
    if len(candidates) == 1:
        return candidates[0]
    # Scheduler/Guider normally have model at slot 0. Fall back only when the
    # graph itself proves there is exactly one MODEL edge into that node.
    inbound = [
        (l.get("target_slot"), l)
        for l in links.values()
        if l.get("target_id") == node.get("id") and str(l.get("type", "")).upper() == "MODEL"
    ]
    return inbound[0] if len(inbound) == 1 else None


def find_final_common_model_chain(container):
    """Find a safe insertion point immediately before Scheduler + Guider.

    This intentionally does *not* require UNETLoader to be the producer. A
    LoRA loader, model-sampling patch, compile wrapper, etc. may sit between the
    loader and the consumers. We insert after the final shared MODEL producer.

    Returns a list because multiple complete sampling chains in one container
    are treated as ambiguous and refused rather than guessed.
    """
    nodes, links = maps(container)
    scheds = [n for n in nodes.values() if n.get("type") == "BasicScheduler"]
    guiders = [n for n in nodes.values() if n.get("type") == "BasicGuider"]
    candidates = []
    for sched in scheds:
        s_info = _model_input_link(sched, links)
        if not s_info:
            continue
        s_slot, s_link = s_info
        for guider in guiders:
            g_info = _model_input_link(guider, links)
            if not g_info:
                continue
            g_slot, g_link = g_info
            # Safe case: both consumers receive the same MODEL output. This
            # covers UNETLoader->..., LoRA chains, ModelSampling chains, etc.
            same_source = (
                s_link.get("origin_id") == g_link.get("origin_id")
                and s_link.get("origin_slot") == g_link.get("origin_slot")
            )
            if not same_source:
                continue
            producer = nodes.get(s_link.get("origin_id"))
            if producer is None or producer.get("type") == TE_NODE_TYPE:
                continue
            candidates.append((producer, sched, guider, s_link, g_link, s_slot, g_slot))
    return candidates


def next_ids(container):
    nodes, links = maps(container)
    state = container.get("state") if isinstance(container.get("state"), dict) else {}
    node_ids = [int(i) for i in nodes if isinstance(i, int)]
    link_ids = [int(i) for i in links if isinstance(i, int)]
    node_id = max([int(state.get("lastNodeId", 0) or 0)] + node_ids) + 1
    link_id = max([int(state.get("lastLinkId", 0) or 0)] + link_ids) + 1
    return node_id, link_id


def patch_container(container):
    """Return (status, message). status: patched/already/skip/conflict."""
    if owned_te_nodes(container):
        return "already", "safe TE-Speed node already present"
    if foreign_te_nodes(container):
        return "skip", "existing TE-Speed node is not owned by this installer"
    candidates = find_final_common_model_chain(container)
    if len(candidates) == 0:
        return "skip", "no shared final MODEL producer feeding Scheduler + Guider"
    if len(candidates) > 1:
        return "conflict", "multiple candidate sampling chains found; refusing to guess"

    producer, sched, guider, l_sched, l_guider, sched_slot, guider_slot = candidates[0]
    new_id, new_link = next_ids(container)
    te = copy.deepcopy(TE_TEMPLATE)
    te["id"] = new_id
    producer_pos = producer.get("pos") or [0, 0]
    te["pos"] = [producer_pos[0] + 520, producer_pos[1]]
    te["inputs"][0]["link"] = l_sched["id"]
    te["outputs"][0]["links"] = [l_guider["id"], new_link]
    te["properties"][RESTORE_KEY] = {
        "producer_id": producer["id"],
        "producer_slot": l_sched.get("origin_slot", 0),
        "scheduler_id": sched["id"],
        "scheduler_slot": sched_slot,
        "guider_id": guider["id"],
        "guider_slot": guider_slot,
        "producer_to_te_link_id": l_sched["id"],
        "te_to_guider_link_id": l_guider["id"],
        "te_to_scheduler_link_id": new_link,
    }
    container["nodes"].append(te)

    # Re-purpose existing edges so unrelated producer fan-out is untouched.
    l_sched["target_id"] = new_id
    l_sched["target_slot"] = 0
    l_guider["origin_id"] = new_id
    l_guider["origin_slot"] = 0
    container["links"].append({
        "id": new_link,
        "origin_id": new_id,
        "origin_slot": 0,
        "target_id": sched["id"],
        "target_slot": sched_slot,
        "type": "MODEL",
    })

    outputs = producer.get("outputs") or []
    pslot = int(l_sched.get("origin_slot", 0) or 0)
    if 0 <= pslot < len(outputs):
        current = list(outputs[pslot].get("links") or [])
        outputs[pslot]["links"] = [x for x in current if x != l_guider["id"]]
    sched_inputs = sched.get("inputs") or []
    if 0 <= sched_slot < len(sched_inputs):
        sched_inputs[sched_slot]["link"] = new_link

    if isinstance(container.get("state"), dict):
        container["state"]["lastNodeId"] = max(int(container["state"].get("lastNodeId", 0) or 0), new_id)
        container["state"]["lastLinkId"] = max(int(container["state"].get("lastLinkId", 0) or 0), new_link)
    return "patched", f"inserted TE-Speed node {new_id} after {producer.get('type')}#{producer.get('id')}"


def revert_one_node(container, te):
    nodes, links = maps(container)
    meta = (te.get("properties") or {}).get(RESTORE_KEY)
    if not isinstance(meta, dict):
        return False, "owned TE-Speed node is missing restore metadata"

    # v2 compatibility: translate old metadata names.
    producer_id = meta.get("producer_id", meta.get("unet_id"))
    producer_to_te_id = meta.get("producer_to_te_link_id", meta.get("unet_to_te_link_id"))
    producer_slot = int(meta.get("producer_slot", 0) or 0)
    scheduler_slot = int(meta.get("scheduler_slot", 0) or 0)
    guider_slot = int(meta.get("guider_slot", 0) or 0)
    try:
        producer = nodes[producer_id]
        sched = nodes[meta["scheduler_id"]]
        guider = nodes[meta["guider_id"]]
        l_in = links[producer_to_te_id]
        l_guider = links[meta["te_to_guider_link_id"]]
        l_sched = links[meta["te_to_scheduler_link_id"]]
    except KeyError as exc:
        return False, f"required node/link no longer exists: {exc}"

    te_id = te["id"]
    te_output_links = set(((te.get("outputs") or [{}])[0].get("links") or []))
    expected = [
        l_in.get("origin_id") == producer["id"] and int(l_in.get("origin_slot", 0) or 0) == producer_slot and l_in.get("target_id") == te_id and l_in.get("target_slot") == 0,
        l_guider.get("origin_id") == te_id and l_guider.get("origin_slot") == 0 and l_guider.get("target_id") == guider["id"] and int(l_guider.get("target_slot", 0) or 0) == guider_slot,
        l_sched.get("origin_id") == te_id and l_sched.get("origin_slot") == 0 and l_sched.get("target_id") == sched["id"] and int(l_sched.get("target_slot", 0) or 0) == scheduler_slot,
        (te.get("inputs") or [{}])[0].get("link") == l_in["id"],
        te_output_links == {l_guider["id"], l_sched["id"]},
    ]
    if not all(expected):
        return False, "TE-Speed wiring was changed after install; refusing to guess"

    l_in["target_id"] = sched["id"]
    l_in["target_slot"] = scheduler_slot
    l_guider["origin_id"] = producer["id"]
    l_guider["origin_slot"] = producer_slot
    container["links"] = [l for l in container.get("links", []) if l.get("id") != l_sched["id"]]

    outputs = producer.get("outputs") or []
    if 0 <= producer_slot < len(outputs):
        current = list(outputs[producer_slot].get("links") or [])
        if l_in["id"] not in current:
            current.append(l_in["id"])
        if l_guider["id"] not in current:
            current.append(l_guider["id"])
        outputs[producer_slot]["links"] = current
    sched_inputs = sched.get("inputs") or []
    if 0 <= scheduler_slot < len(sched_inputs):
        sched_inputs[scheduler_slot]["link"] = l_in["id"]

    container["nodes"] = [n for n in container.get("nodes", []) if n.get("id") != te_id]
    return True, f"removed TE-Speed node {te_id} and restored shared MODEL wiring"


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
                print(f"[CONFLICT] {path.name} / {container_label(container, is_root)}: found {len(foreign)} unowned TE-Speed node(s)")
            for te in list(owned_te_nodes(container)):
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
            print(f"[HOLD] {path.name}: not written because a rollback conflict was detected")
    print(f"TE-Speed workflow revert summary: changed_files={changed_files}, conflicts={conflicts}")
    return 2 if conflicts else 0


def cmd_check(files):
    owned = foreign = missing = relevant = 0
    for path in files:
        try:
            wf, _, _ = read_workflow(path)
        except Exception as exc:
            print(f"[WARN] {path.name}: unreadable workflow JSON ({exc})")
            continue
        for container, is_root in iter_containers(wf):
            if not is_h3_container(container):
                continue
            relevant += 1
            o = len(owned_te_nodes(container))
            f = len(foreign_te_nodes(container))
            if o == 0 and f == 0:
                missing += 1
                print(f"[OFF] {path.name} / {container_label(container, is_root)}: H3 chain has no TE-Speed node")
            else:
                print(f"[CHECK] {path.name} / {container_label(container, is_root)}: owned={o}, other={f}")
            owned += o
            foreign += f
    print(f"TE-Speed workflow check: h3_containers={relevant}, owned={owned}, other={foreign}, missing={missing}")
    if foreign or missing:
        return 1
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
