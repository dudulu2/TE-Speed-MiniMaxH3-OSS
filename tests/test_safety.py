import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = ROOT / "TE-Speed-MiniMaxH3-OSS"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = load_module("patch_model", NODE / "patch_model.py")
wfmod = load_module("wf_patch", NODE / "tespeed_workflow_patch.py")


STOCK_MODEL = '''class MiniMaxH3Model:\n    def __init__(self):\n        self.blocks = []\n\n    def forward(self, x, timestep, context, transformer_options={}, minimax_payload=None, **kwargs):\n        return self._forward(x, timestep, context, transformer_options, minimax_payload, **kwargs)\n\n    def _forward(self, x, timestep, context, transformer_options={}, minimax_payload=None, **kwargs):\n        device = x.device\n        h = x\n        t_emb = None\n        mod_segments = []\n        rope_freqs = None\n        patches_replace = transformer_options.get("patches_replace", {})\n        blocks_replace = patches_replace.get("dit", {})\n        prefetch_queue = comfy.model_prefetch.make_prefetch_queue(list(self.blocks), device, transformer_options)\n        for i, block in enumerate(self.blocks):\n            comfy.model_prefetch.prefetch_queue_pop(prefetch_queue, device, block)\n            if ("double_block", i) in blocks_replace:\n                def block_wrap(args):\n                    return {"img": block(args["img"], args["t_emb"], args["mod_segments"], args["rope_freqs"],\n                                         transformer_options=args["transformer_options"])}\n                h = blocks_replace[("double_block", i)](\n                    {"img": h, "t_emb": t_emb, "mod_segments": mod_segments, "rope_freqs": rope_freqs,\n                     "transformer_options": transformer_options},\n                    {"original_block": block_wrap})["img"]\n            else:\n                h = block(h, t_emb, mod_segments, rope_freqs, transformer_options=transformer_options)\n        if prefetch_queue is not None:\n            comfy.model_prefetch.prefetch_queue_pop(prefetch_queue, device, None)\n        return h\n'''


def workflow_fixture():
    return {
        "nodes": [
            {"id": 1, "type": "UNETLoader", "pos": [0, 0], "inputs": [], "outputs": [{"name": "MODEL", "links": [10, 11]}]},
            {"id": 2, "type": "BasicScheduler", "pos": [700, 0], "inputs": [{"name": "model", "link": 10}], "outputs": []},
            {"id": 3, "type": "BasicGuider", "pos": [700, 200], "inputs": [{"name": "model", "link": 11}], "outputs": []},
            {"id": 4, "type": "MiniMaxH3TextToVideo", "pos": [0, 300], "inputs": [], "outputs": []},
        ],
        "links": [
            {"id": 10, "origin_id": 1, "origin_slot": 0, "target_id": 2, "target_slot": 0, "type": "MODEL"},
            {"id": 11, "origin_id": 1, "origin_slot": 0, "target_id": 3, "target_slot": 0, "type": "MODEL"},
        ],
        "state": {"lastNodeId": 4, "lastLinkId": 11},
    }


class ModelPatchSafetyTests(unittest.TestCase):
    def test_round_trip_preserves_unrelated_later_edit(self):
        with tempfile.TemporaryDirectory() as td:
            comfy = Path(td) / "ComfyUI"
            target = comfy / "comfy" / "ldm" / "minimax" / "model.py"
            target.parent.mkdir(parents=True)
            target.write_text(STOCK_MODEL, encoding="utf-8")
            self.assertEqual(pm.install(target), 0)
            patched = target.read_text(encoding="utf-8")
            self.assertIn(pm.RUN_BEGIN, patched)
            # Simulate a later unrelated ComfyUI/user edit outside our marked regions.
            patched = "# unrelated later edit\n" + patched
            target.write_text(patched, encoding="utf-8")
            self.assertEqual(pm.revert(target), 0)
            restored = target.read_text(encoding="utf-8")
            self.assertTrue(restored.startswith("# unrelated later edit\n"))
            self.assertIn('prefetch_queue = comfy.model_prefetch.make_prefetch_queue(list(self.blocks), device, transformer_options)', restored)
            self.assertNotIn(pm.RUN_BEGIN, restored)
            self.assertNotIn(pm.LOOP_BEGIN, restored)

    def test_revert_refuses_when_owned_region_edited(self):
        with tempfile.TemporaryDirectory() as td:
            comfy = Path(td) / "ComfyUI"
            target = comfy / "comfy" / "ldm" / "minimax" / "model.py"
            target.parent.mkdir(parents=True)
            target.write_text(STOCK_MODEL, encoding="utf-8")
            pm.install(target)
            text = target.read_text(encoding="utf-8")
            target.write_text(text.replace("cache_ranges =", "cache_ranges =  # changed\n        #"), encoding="utf-8")
            with self.assertRaises(SystemExit):
                pm.revert(target)
            self.assertIn(pm.LOOP_BEGIN, target.read_text(encoding="utf-8"))


class WorkflowSafetyTests(unittest.TestCase):
    def test_top_level_add_and_surgical_revert_preserve_other_edits(self):
        wf = workflow_fixture()
        status, _ = wfmod.patch_container(wf)
        self.assertEqual(status, "patched")
        te = wfmod.owned_te_nodes(wf)[0]
        # Later user edit unrelated to TE wiring.
        wf["nodes"].append({"id": 99, "type": "UserAddedNode", "pos": [1, 1], "inputs": [], "outputs": []})
        ok, _ = wfmod.revert_one_node(wf, te)
        self.assertTrue(ok)
        self.assertTrue(any(n.get("id") == 99 for n in wf["nodes"]))
        self.assertFalse(wfmod.owned_te_nodes(wf))
        links = {l["id"]: l for l in wf["links"]}
        self.assertEqual((links[10]["origin_id"], links[10]["target_id"]), (1, 2))
        self.assertEqual((links[11]["origin_id"], links[11]["target_id"]), (1, 3))

    def test_revert_refuses_changed_te_wiring(self):
        wf = workflow_fixture()
        status, _ = wfmod.patch_container(wf)
        self.assertEqual(status, "patched")
        te = wfmod.owned_te_nodes(wf)[0]
        meta = te["properties"][wfmod.RESTORE_KEY]
        # User rewired the TE -> scheduler link after install.
        for link in wf["links"]:
            if link["id"] == meta["te_to_scheduler_link_id"]:
                link["target_id"] = 999
        ok, msg = wfmod.revert_one_node(wf, te)
        self.assertFalse(ok)
        self.assertIn("refusing", msg)
        self.assertTrue(wfmod.owned_te_nodes(wf))


if __name__ == "__main__":
    unittest.main()
