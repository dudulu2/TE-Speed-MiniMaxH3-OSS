#!/usr/bin/env python
"""Safely add/remove the MiniMax H3 block-loop hook used by TE-Speed.

Safety rules:
- Never blindly restore an old whole-file backup during normal uninstall.
- Install only when the expected stock block loop is found exactly enough to patch.
- Mark every injected region.
- Save the exact original block-loop text plus hashes in ComfyUI/.te_speed_minimaxh3.
- Revert only our marked regions; preserve unrelated edits made after installation.
- If a marked region was itself edited, stop instead of guessing/overwriting.
- --preflight validates compatibility without writing anything.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

TARGET = Path("comfy/ldm/minimax/model.py")
STATE_DIR_NAME = ".te_speed_minimaxh3"
STATE_FILE_NAME = "model_patch_state.json"
PATCH_VERSION = 3

RUN_BEGIN = "    # >>> TE_SPEED_MINIMAXH3_SAFE_V3:RUN_BLOCKS_BEGIN"
RUN_END = "    # <<< TE_SPEED_MINIMAXH3_SAFE_V3:RUN_BLOCKS_END"
LOOP_BEGIN = "        # >>> TE_SPEED_MINIMAXH3_SAFE_V3:BLOCK_LOOP_BEGIN"
LOOP_END = "        # <<< TE_SPEED_MINIMAXH3_SAFE_V3:BLOCK_LOOP_END"

# V2 markers are accepted for safe migration/uninstall.
V2_RUN_BEGIN = "    # >>> TE_SPEED_MINIMAXH3_SAFE_V2:RUN_BLOCKS_BEGIN"
V2_RUN_END = "    # <<< TE_SPEED_MINIMAXH3_SAFE_V2:RUN_BLOCKS_END"
V2_LOOP_BEGIN = "        # >>> TE_SPEED_MINIMAXH3_SAFE_V2:BLOCK_LOOP_BEGIN"
V2_LOOP_END = "        # <<< TE_SPEED_MINIMAXH3_SAFE_V2:BLOCK_LOOP_END"

RUN_BLOCKS_BODY = '''    def _run_blocks(self, h, t_emb, mod_segments, rope_freqs, transformer_options, start=0, end=None):
        patches_replace = transformer_options.get("patches_replace", {})
        blocks_replace = patches_replace.get("dit", {})
        end = len(self.blocks) if end is None else end
        prefetch_queue = comfy.model_prefetch.make_prefetch_queue(list(self.blocks[start:end]), h.device, transformer_options)
        for i in range(start, end):
            block = self.blocks[i]
            comfy.model_prefetch.prefetch_queue_pop(prefetch_queue, h.device, block)
            if ("double_block", i) in blocks_replace:
                def block_wrap(args):
                    return {"img": block(args["img"], args["t_emb"], args["mod_segments"], args["rope_freqs"],
                                         transformer_options=args["transformer_options"])}
                h = blocks_replace[("double_block", i)](
                    {"img": h, "t_emb": t_emb, "mod_segments": mod_segments, "rope_freqs": rope_freqs,
                     "transformer_options": transformer_options},
                    {"original_block": block_wrap})["img"]
            else:
                h = block(h, t_emb, mod_segments, rope_freqs, transformer_options=transformer_options)
        if prefetch_queue is not None:
            comfy.model_prefetch.prefetch_queue_pop(prefetch_queue, h.device, None)
        return h
'''

HOOK_LOOP_BODY = '''        patches_replace = transformer_options.get("patches_replace", {})
        blocks_replace = patches_replace.get("dit", {})
        cache_ranges = [(a, b) for a, b, kind in layout.segments if kind in ("audio", "video")]
        if ("block_loop", 0) in blocks_replace:
            def block_loop_wrap(args):
                return {"img": self._run_blocks(args["img"], args["t_emb"], args["mod_segments"], args["rope_freqs"],
                                                args["transformer_options"], args.get("start", 0), args.get("end"))}
            h = blocks_replace[("block_loop", 0)](
                {"img": h, "t_emb": t_emb, "mod_segments": mod_segments, "rope_freqs": rope_freqs,
                 "transformer_options": transformer_options, "cache_ranges": cache_ranges, "block_count": len(self.blocks)},
                {"original_block": block_loop_wrap})["img"]
        else:
            h = self._run_blocks(h, t_emb, mod_segments, rope_freqs, transformer_options)
'''

RUN_BLOCKS_MARKED = RUN_BEGIN + "\n" + RUN_BLOCKS_BODY + RUN_END + "\n\n"
HOOK_LOOP_MARKED = LOOP_BEGIN + "\n" + HOOK_LOOP_BODY + LOOP_END + "\n"

LOOP_RE = re.compile(
    r'        patches_replace = transformer_options\.get\("patches_replace", \{\}\)\n'
    r'        blocks_replace = patches_replace\.get\("dit", \{\}\)\n'
    r'        prefetch_queue = comfy\.model_prefetch\.make_prefetch_queue\('
    r'list\(self\.blocks\), device, transformer_options\)\n'
    r'.*?'
    r'        if prefetch_queue is not None:\n'
    r'            comfy\.model_prefetch\.prefetch_queue_pop\(prefetch_queue, device, None\)\n',
    re.DOTALL,
)

FORWARD_ANCHOR = "    def forward(self, x, timestep, context,"
LEGACY_RUN_ANCHOR = "    def _run_blocks(self, h, t_emb, mod_segments, rope_freqs, transformer_options, start=0, end=None):"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def normalize_newlines(raw: bytes) -> tuple[str, str]:
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def encode_with_newline(text: str, newline: str) -> bytes:
    if newline != "\n":
        text = text.replace("\n", newline)
    return text.encode("utf-8")


def find_model_file(comfy_ui=None) -> Path:
    if comfy_ui is not None:
        p = Path(comfy_ui)
        candidates = [p / TARGET, p / "ComfyUI" / TARGET]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise SystemExit(f"error: no model file below {p}")
    raise SystemExit("error: pass --comfy-ui <ComfyUI root>")


def comfy_root_from_target(target: Path) -> Path:
    return target.parents[3]


def state_paths(target: Path) -> tuple[Path, Path]:
    state_dir = comfy_root_from_target(target) / STATE_DIR_NAME
    return state_dir, state_dir / STATE_FILE_NAME


def marker_set(text: str):
    if all(x in text for x in (RUN_BEGIN, RUN_END, LOOP_BEGIN, LOOP_END)):
        return "v3", (RUN_BEGIN, RUN_END, LOOP_BEGIN, LOOP_END)
    if all(x in text for x in (V2_RUN_BEGIN, V2_RUN_END, V2_LOOP_BEGIN, V2_LOOP_END)):
        return "v2", (V2_RUN_BEGIN, V2_RUN_END, V2_LOOP_BEGIN, V2_LOOP_END)
    return None, None


def has_safe_patch(text: str) -> bool:
    return marker_set(text)[0] is not None


def has_any_safe_marker(text: str) -> bool:
    markers = (RUN_BEGIN, RUN_END, LOOP_BEGIN, LOOP_END, V2_RUN_BEGIN, V2_RUN_END, V2_LOOP_BEGIN, V2_LOOP_END)
    return any(marker in text for marker in markers)


def has_legacy_patch(text: str) -> bool:
    return LEGACY_RUN_ANCHOR in text and '("block_loop", 0) in blocks_replace' in text and not has_safe_patch(text)


def extract_marked(text: str, begin: str, end: str) -> str | None:
    start = text.find(begin)
    if start < 0:
        return None
    finish = text.find(end, start)
    if finish < 0:
        return None
    finish += len(end)
    if finish < len(text) and text[finish] == "\n":
        finish += 1
    return text[start:finish]


def save_state(target: Path, original_loop: str, pre_raw: bytes, patched_raw: bytes) -> None:
    state_dir, state_file = state_paths(target)
    recovery = state_dir / "recovery" / "model"
    recovery.mkdir(parents=True, exist_ok=True)
    backup = recovery / f"model.py.{utc_stamp()}.{sha256_bytes(pre_raw)[:12]}.bak"
    if not backup.exists():
        backup.write_bytes(pre_raw)
    state = {
        "version": PATCH_VERSION,
        "target": str(TARGET).replace("\\", "/"),
        "installed_at_utc": utc_stamp(),
        "pre_sha256": sha256_bytes(pre_raw),
        "patched_sha256": sha256_bytes(patched_raw),
        "original_loop": original_loop,
        "run_region_sha256": sha256_text(RUN_BLOCKS_MARKED.rstrip("\n")),
        "loop_region_sha256": sha256_text(HOOK_LOOP_MARKED.rstrip("\n")),
        "recovery_backup": str(backup),
    }
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state(target: Path) -> dict:
    _, state_file = state_paths(target)
    if not state_file.is_file():
        raise SystemExit("error: safe patch state is missing; current model.py was left untouched")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    if state.get("version") not in {2, 3} or not isinstance(state.get("original_loop"), str):
        raise SystemExit("error: incompatible patch state; current model.py was left untouched")
    return state


def apply_patch(text: str) -> tuple[str, str]:
    if has_any_safe_marker(text):
        if has_safe_patch(text):
            return text, ""
        raise SystemExit("error: partial TE-Speed safe markers found; refusing to guess")
    if has_legacy_patch(text):
        raise SystemExit("error: legacy TE-Speed patch detected; uninstall the legacy package first")
    match = LOOP_RE.search(text)
    if not match:
        if '("block_loop", 0) in blocks_replace' in text:
            raise SystemExit("error: model.py already has an unowned block_loop implementation; left untouched")
        raise SystemExit("error: expected stock MiniMax H3 block loop was not found; left untouched")
    if FORWARD_ANCHOR not in text:
        raise SystemExit("error: MiniMaxH3Model.forward anchor not found; left untouched")
    original_loop = match.group(0)
    patched = text[:match.start()] + HOOK_LOOP_MARKED + text[match.end():]
    patched = patched.replace(FORWARD_ANCHOR, RUN_BLOCKS_MARKED + FORWARD_ANCHOR, 1)
    ast.parse(patched)
    if not has_safe_patch(patched):
        raise SystemExit("error: internal patch verification failed")
    return patched, original_loop


def preflight(target: Path) -> int:
    text, _ = normalize_newlines(target.read_bytes())
    if has_safe_patch(text):
        print(f"[OK] safe TE-Speed hooks already present: {target}")
        return 0
    try:
        patched, _ = apply_patch(text)
        ast.parse(patched)
    except SystemExit as exc:
        print(str(exc))
        return 2
    print(f"[OK] MiniMax H3 core is compatible with TE-Speed safe patch: {target}")
    return 0


def revert_patch(text: str, state: dict) -> str:
    version, markers = marker_set(text)
    if version is None:
        if not has_any_safe_marker(text):
            if has_legacy_patch(text):
                raise SystemExit("error: legacy/untracked TE-Speed patch detected; refusing unsafe rollback")
            print("TE-Speed: safe patch markers are absent; model.py already appears unpatched.")
            return text
        raise SystemExit("error: partial safe markers found; refusing unsafe rollback")

    run_begin, run_end, loop_begin, loop_end = markers
    run_region = extract_marked(text, run_begin, run_end)
    loop_region = extract_marked(text, loop_begin, loop_end)
    if run_region is None or loop_region is None:
        raise SystemExit("error: could not isolate safe patch regions; left untouched")

    # V3 verifies exact owned regions. V2 state also contains hashes; retain the
    # same protection for migration/uninstall.
    if sha256_text(run_region.rstrip("\n")) != state.get("run_region_sha256"):
        raise SystemExit("error: TE-Speed _run_blocks region was edited after install; left untouched")
    if sha256_text(loop_region.rstrip("\n")) != state.get("loop_region_sha256"):
        raise SystemExit("error: TE-Speed block-loop region was edited after install; left untouched")

    out = text.replace(run_region, "", 1)
    out = out.replace(loop_region, state["original_loop"], 1)
    ast.parse(out)
    if has_any_safe_marker(out):
        raise SystemExit("error: rollback verification failed; left untouched")
    return out


def install(target: Path) -> int:
    pre_raw = target.read_bytes()
    text, newline = normalize_newlines(pre_raw)
    if has_safe_patch(text):
        print(f"TE-Speed: safe hooks already installed in {target}")
        return 0
    patched, original_loop = apply_patch(text)
    patched_raw = encode_with_newline(patched, newline)
    save_state(target, original_loop, pre_raw, patched_raw)
    target.write_bytes(patched_raw)
    verify, _ = normalize_newlines(target.read_bytes())
    if not has_safe_patch(verify):
        raise SystemExit("error: write verification failed")
    print(f"TE-Speed: safely patched {target}")
    return 0


def revert(target: Path) -> int:
    current_raw = target.read_bytes()
    text, newline = normalize_newlines(current_raw)
    if not has_any_safe_marker(text) and not has_legacy_patch(text):
        print(f"TE-Speed: {target} is already unpatched; nothing to do")
        return 0
    state = load_state(target)
    restored = revert_patch(text, state)
    if restored == text:
        return 0
    target.write_bytes(encode_with_newline(restored, newline))
    print("TE-Speed: removed only TE-Speed model hooks; unrelated model.py edits were preserved.")
    return 0


def check(target: Path) -> int:
    text, _ = normalize_newlines(target.read_bytes())
    version, _ = marker_set(text)
    if version:
        print(f"[ON] safe TE-Speed hooks present ({version}): {target}")
        return 0
    if has_legacy_patch(text):
        print(f"[WARN] legacy/untracked TE-Speed hooks present: {target}")
        return 2
    if has_any_safe_marker(text):
        print(f"[ERROR] partial safe markers present: {target}")
        return 2
    print(f"[OFF] TE-Speed safe hooks absent: {target}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe MiniMax H3 TE-Speed model hook manager")
    parser.add_argument("--comfy-ui", help="ComfyUI root directory")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--revert", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    target = find_model_file(args.comfy_ui)
    try:
        if args.revert:
            code = revert(target)
        elif args.check:
            code = check(target)
        elif args.preflight:
            code = preflight(target)
        else:
            code = install(target)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"error: {exc}")
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
