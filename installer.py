#!/usr/bin/env python
"""Transaction-oriented one-click installer for TE-Speed-MiniMaxH3-OSS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_DIR_NAME = "TE-Speed-MiniMaxH3-OSS"
STATE_DIR_NAME = ".te_speed_minimaxh3"
NODE_MANIFEST = "node_manifest.json"
MANIFEST_VERSION = 2


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def source_files(src: Path):
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if "__pycache__" in rel.parts or p.suffix.lower() == ".pyc":
            continue
        yield rel, p


def validate_root(root: Path):
    root = root.resolve()
    comfy = root / "ComfyUI"
    model = comfy / "comfy" / "ldm" / "minimax" / "model.py"
    if not model.is_file():
        raise RuntimeError(f"not a MiniMaxH3/ComfyUI root: missing {model}")
    return root, comfy


def state_dir(comfy: Path) -> Path:
    p = comfy / STATE_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_manifest(comfy: Path):
    p = state_dir(comfy) / NODE_MANIFEST
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid node manifest {p}: {exc}")
    if data.get("version") != MANIFEST_VERSION or not isinstance(data.get("files"), dict):
        raise RuntimeError(f"unsupported node manifest {p}")
    return data


def save_manifest(comfy: Path, files: dict[str, str]):
    p = state_dir(comfy) / NODE_MANIFEST
    p.write_text(json.dumps({
        "version": MANIFEST_VERSION,
        "installed_at_utc": utc_stamp(),
        "files": files,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def preflight_node_sync(src: Path, dst: Path, comfy: Path):
    manifest = load_manifest(comfy)
    old = (manifest or {}).get("files", {})
    conflicts = []
    src_map = {str(rel).replace("\\", "/"): path for rel, path in source_files(src)}
    if dst.exists():
        for rel_s, sp in src_map.items():
            dp = dst / Path(rel_s)
            if not dp.exists():
                continue
            current = sha256_file(dp)
            desired = sha256_file(sp)
            tracked = old.get(rel_s)
            if current == desired:
                continue
            if tracked and current == tracked:
                continue
            conflicts.append(rel_s)
    if conflicts:
        joined = ", ".join(conflicts[:8]) + (" ..." if len(conflicts) > 8 else "")
        raise RuntimeError(
            "custom node destination contains untracked/user-modified files; refusing to overwrite: " + joined
        )
    return src_map


def sync_node(src: Path, dst: Path, comfy: Path):
    src_map = preflight_node_sync(src, dst, comfy)
    recovery_root = state_dir(comfy) / "recovery" / "custom_node" / utc_stamp()
    installed = {}
    for rel_s, sp in src_map.items():
        dp = dst / Path(rel_s)
        dp.parent.mkdir(parents=True, exist_ok=True)
        if dp.exists() and sha256_file(dp) != sha256_file(sp):
            backup = recovery_root / Path(rel_s)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dp, backup)
        shutil.copy2(sp, dp)
        installed[rel_s] = sha256_file(dp)
    save_manifest(comfy, installed)
    print(f"[OK] custom node files synced: {dst}")
    return installed


def remove_owned_node(dst: Path, comfy: Path):
    manifest = load_manifest(comfy)
    if not manifest:
        print("[WARN] node manifest is missing; node folder was left untouched for safety")
        return 1
    kept = []
    removed = 0
    for rel_s, expected in manifest["files"].items():
        dp = dst / Path(rel_s)
        if not dp.exists():
            continue
        if sha256_file(dp) != expected:
            kept.append(rel_s)
            continue
        dp.unlink()
        removed += 1
    if dst.exists():
        for d in sorted([p for p in dst.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            dst.rmdir()
        except OSError:
            pass
    if kept:
        print("[WARN] modified/untracked node files were preserved: " + ", ".join(kept[:8]))
        return 1
    print(f"[OK] removed {removed} installer-owned node files")
    return 0



def workflow_dirs(comfy: Path):
    user = comfy / "user"
    if not user.is_dir():
        return []
    dirs = sorted({p for p in user.rglob("workflows") if p.is_dir()})
    return dirs

def run_py(script: Path, *args: str) -> int:
    cmd = [sys.executable, str(script), *map(str, args)]
    print("[RUN] " + " ".join(f'"{x}"' if " " in x else x for x in cmd))
    proc = subprocess.run(cmd)
    return proc.returncode


def install(root: Path, package_root: Path) -> int:
    root, comfy = validate_root(root)
    src = package_root / PACKAGE_DIR_NAME
    if not (src / "nodes.py").is_file():
        raise RuntimeError(f"package incomplete: {src / 'nodes.py'} missing")
    dst = comfy / "custom_nodes" / PACKAGE_DIR_NAME
    workflows = workflow_dirs(comfy)

    # Preflight BEFORE touching anything.
    preflight_node_sync(src, dst, comfy)

    sync_node(src, dst, comfy)

    code = run_py(src / "patch_model.py", "--comfy-ui", str(comfy))
    if code != 0:
        print("[ERROR] model hook install failed. Workflows were not changed.")
        print("        Node files may remain, but they are inert without the model hook.")
        return 2

    wf_warning = False
    if workflows:
        code = run_py(src / "tespeed_workflow_patch.py", "--add", *[str(p) for p in workflows])
        if code != 0:
            wf_warning = True
            print("[WARN] one or more workflows were not auto-wired because the script refused an ambiguous layout.")
    else:
        print("[WARN] no ComfyUI user workflow directories found; workflow auto-wiring skipped")
        wf_warning = True

    print("[OK] TE-Speed core installation is complete.")
    if wf_warning:
        print("[WARN] Acceleration is active only in workflows that contain the TESpeedMiniMaxH3 node.")
        return 1
    print("[OK] Matching MiniMax H3 workflows were wired automatically.")
    return 0


def uninstall(root: Path, package_root: Path) -> int:
    root, comfy = validate_root(root)
    src = package_root / PACKAGE_DIR_NAME
    dst = comfy / "custom_nodes" / PACKAGE_DIR_NAME
    workflows = workflow_dirs(comfy)

    # First remove workflow dependency. If any owned TE wiring was edited by the user,
    # STOP and keep model hook + node installed so those workflows do not break.
    if workflows:
        code = run_py(src / "tespeed_workflow_patch.py", "--revert", *[str(p) for p in workflows])
        if code != 0:
            print("[STOP] A workflow changed its TE-Speed wiring after installation.")
            print("       Nothing else was removed. Fix/reconnect that workflow, then run uninstall again.")
            return 2

    # Next remove only our marked model.py regions. Never restore a stale whole file.
    code = run_py(src / "patch_model.py", "--revert", "--comfy-ui", str(comfy))
    if code != 0:
        print("[STOP] model.py safe rollback refused. The custom node was kept installed.")
        return 2

    warn = remove_owned_node(dst, comfy)
    print("[OK] Safe uninstall finished: workflows/model.py were surgically reverted.")
    return 1 if warn else 0


def check(root: Path, package_root: Path) -> int:
    root, comfy = validate_root(root)
    src = package_root / PACKAGE_DIR_NAME
    workflows = workflow_dirs(comfy)
    c1 = run_py(src / "patch_model.py", "--check", "--comfy-ui", str(comfy))
    c2 = 0
    if workflows:
        c2 = run_py(src / "tespeed_workflow_patch.py", "--check", *[str(p) for p in workflows])
    return 0 if c1 == 0 and c2 == 0 else 1


def main():
    parser = argparse.ArgumentParser(description="MiniMax H3 TE-Speed safe one-click installer")
    parser.add_argument("action", choices=["install", "uninstall", "check"])
    parser.add_argument("--root", required=True, help="MiniMaxH3 bundle root containing ComfyUI and runtime")
    args = parser.parse_args()
    package_root = Path(__file__).resolve().parent
    try:
        if args.action == "install":
            code = install(Path(args.root), package_root)
        elif args.action == "uninstall":
            code = uninstall(Path(args.root), package_root)
        else:
            code = check(Path(args.root), package_root)
    except Exception as exc:
        print(f"[ERROR] {exc}")
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
