#!/usr/bin/env python
"""Transaction-oriented one-click installer for TE-Speed-MiniMaxH3-OSS."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_DIR_NAME = "TE-Speed-MiniMaxH3-OSS"
STATE_DIR_NAME = ".te_speed_minimaxh3"
NODE_MANIFEST = "node_manifest.json"
MANIFEST_VERSION = 3


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
    """Accept either <bundle root> or the ComfyUI root itself."""
    root = root.resolve()
    candidates = [root, root / "ComfyUI"]
    for comfy in candidates:
        model = comfy / "comfy" / "ldm" / "minimax" / "model.py"
        if model.is_file() and (comfy / "custom_nodes").is_dir():
            return root, comfy
    raise RuntimeError(
        f"not a recognized MiniMaxH3/ComfyUI root: {root}; expected comfy/ldm/minimax/model.py and custom_nodes"
    )


def state_dir(comfy: Path) -> Path:
    p = comfy / STATE_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def manifest_path(comfy: Path) -> Path:
    return state_dir(comfy) / NODE_MANIFEST


def load_manifest(comfy: Path):
    p = manifest_path(comfy)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid node manifest {p}: {exc}")
    if data.get("version") not in {2, 3} or not isinstance(data.get("files"), dict):
        raise RuntimeError(f"unsupported node manifest {p}")
    return data


def save_manifest(comfy: Path, files: dict[str, str]):
    p = manifest_path(comfy)
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


def remove_owned_node(dst: Path, comfy: Path, *, quiet_missing=False):
    manifest = load_manifest(comfy)
    if not manifest:
        if not quiet_missing:
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
    try:
        manifest_path(comfy).unlink()
    except FileNotFoundError:
        pass
    print(f"[OK] removed {removed} installer-owned node files")
    return 0


def check_node(dst: Path, comfy: Path) -> int:
    manifest = load_manifest(comfy)
    if not manifest:
        print("[OFF] custom node manifest missing")
        return 1
    missing = []
    changed = []
    for rel_s, expected in manifest["files"].items():
        dp = dst / Path(rel_s)
        if not dp.is_file():
            missing.append(rel_s)
        elif sha256_file(dp) != expected:
            changed.append(rel_s)
    if missing:
        print("[ERROR] custom node files missing: " + ", ".join(missing[:8]))
    if changed:
        print("[ERROR] custom node files changed since install: " + ", ".join(changed[:8]))
    if missing or changed:
        return 1
    required = [dst / "__init__.py", dst / "nodes.py", dst / "patch_model.py", dst / "tespeed_workflow_patch.py"]
    absent = [str(p.name) for p in required if not p.is_file()]
    if absent:
        print("[ERROR] required custom node payload missing: " + ", ".join(absent))
        return 1
    print(f"[ON] custom node manifest/hash check passed: {dst}")
    return 0


def workflow_dirs(comfy: Path):
    user = comfy / "user"
    if not user.is_dir():
        return []
    return sorted({p for p in user.rglob("workflows") if p.is_dir()})


def run_py(script: Path, *args: str) -> int:
    cmd = [sys.executable, str(script), *map(str, args)]
    print("[RUN] " + " ".join(f'"{x}"' if " " in x else x for x in cmd))
    proc = subprocess.run(cmd)
    return proc.returncode


def install(root: Path, package_root: Path) -> int:
    _, comfy = validate_root(root)
    src = package_root / PACKAGE_DIR_NAME
    if not (src / "nodes.py").is_file():
        raise RuntimeError(f"package incomplete: {src / 'nodes.py'} missing")
    dst = comfy / "custom_nodes" / PACKAGE_DIR_NAME
    workflows = workflow_dirs(comfy)

    # Full preflight before modifying anything we own.
    preflight_node_sync(src, dst, comfy)
    preflight = run_py(src / "patch_model.py", "--preflight", "--comfy-ui", str(comfy))
    if preflight != 0:
        print("[STOP] MiniMax H3 core is not patch-compatible; nothing was installed.")
        return 2

    node_synced = False
    model_patched = False
    try:
        sync_node(src, dst, comfy)
        node_synced = True

        code = run_py(src / "patch_model.py", "--comfy-ui", str(comfy))
        if code != 0:
            raise RuntimeError("model hook install failed")
        model_patched = True

        wf_warning = False
        if workflows:
            code = run_py(src / "tespeed_workflow_patch.py", "--add", *[str(p) for p in workflows])
            if code != 0:
                wf_warning = True
                print("[WARN] one or more workflows were ambiguous and were not auto-wired.")
        else:
            print("[WARN] no ComfyUI user workflow directories found; workflow auto-wiring skipped")
            wf_warning = True

        print("[OK] TE-Speed core installation is complete.")
        return 1 if wf_warning else 0
    except Exception as exc:
        print(f"[ERROR] installation failed: {exc}")
        print("[ROLLBACK] attempting to remove only changes made by this installation...")
        if model_patched:
            run_py(src / "patch_model.py", "--revert", "--comfy-ui", str(comfy))
        if node_synced:
            remove_owned_node(dst, comfy, quiet_missing=True)
        return 2


def uninstall(root: Path, package_root: Path) -> int:
    _, comfy = validate_root(root)
    src = package_root / PACKAGE_DIR_NAME
    dst = comfy / "custom_nodes" / PACKAGE_DIR_NAME
    workflows = workflow_dirs(comfy)

    if workflows:
        code = run_py(src / "tespeed_workflow_patch.py", "--revert", *[str(p) for p in workflows])
        if code != 0:
            print("[STOP] A workflow changed its TE-Speed wiring after installation.")
            print("       Nothing else was removed. Fix/reconnect that workflow, then run uninstall again.")
            return 2

    code = run_py(src / "patch_model.py", "--revert", "--comfy-ui", str(comfy))
    if code != 0:
        print("[STOP] model.py safe rollback refused. The custom node was kept installed.")
        return 2

    warn = remove_owned_node(dst, comfy)
    print("[OK] Safe uninstall finished: workflows/model.py were surgically reverted.")
    return 1 if warn else 0


def check(root: Path, package_root: Path) -> int:
    _, comfy = validate_root(root)
    src = package_root / PACKAGE_DIR_NAME
    dst = comfy / "custom_nodes" / PACKAGE_DIR_NAME
    workflows = workflow_dirs(comfy)

    print("=== TE-Speed V3 health check ===")
    c_model = run_py(src / "patch_model.py", "--check", "--comfy-ui", str(comfy))
    c_node = check_node(dst, comfy)
    c_workflow = 0
    if workflows:
        c_workflow = run_py(src / "tespeed_workflow_patch.py", "--check", *[str(p) for p in workflows])
    else:
        print("[INFO] no user workflow directories found; workflow check not applicable")

    if c_model == 0 and c_node == 0 and c_workflow == 0:
        print("[PASS] TE-Speed installation is healthy.")
        return 0
    print(f"[WARN] health check failed: model={c_model}, node={c_node}, workflow={c_workflow}")
    return 1


def main():
    parser = argparse.ArgumentParser(description="MiniMax H3 TE-Speed safe one-click installer")
    parser.add_argument("action", choices=["install", "uninstall", "check"])
    parser.add_argument("--root", required=True, help="MiniMaxH3 bundle root or ComfyUI root")
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
