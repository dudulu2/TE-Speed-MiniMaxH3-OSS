"""TE-Speed-MiniMaxH3-OSS: open-source block-cache accelerator for MiniMax H3."""

import torch


class _MiniMaxH3Cache:
    """Block-loop cache: decides between FULL and CACHE execution per step."""

    AUTO_GPU_MIN_BYTES = 20 * 1024**3

    def __init__(self, control_value, start_percent, end_percent, mcs, device, cache_depth=0.75):
        self.threshold = float(control_value)
        self.start_percent = float(start_percent)
        self.end_percent = float(end_percent)
        self.mcs = int(mcs)
        self.cache_device = str(device).lower()
        self.cache_depth = float(cache_depth)
        self.enabled = (self.threshold > 0.0) and (self.mcs > 0) and (self.cache_depth > 0.0)
        self.reset()

    def reset(self):
        self.start_sigma = None
        self.last_sigma = None
        self.prev_sigma = None
        self.step = -1
        self.total_steps = None
        self.consecutive_skips = 0
        self.residual = None
        self.snapshot = None
        self.sigma_scale = 1.0
        self.last_mode = "full"
        self.full_steps = 0
        self.cache_hits = 0
        self.skipped_blocks = 0
        self.total_blocks = 0
        self.printed = False
        self.resolved_cache_device = None

    def __call__(self, args, kwargs):
        original_block = kwargs["original_block"]
        block_count = int(args["block_count"])
        sigma = self._current_sigma(args)
        if sigma is None:
            return {"img": original_block(args)["img"]}

        if self.last_sigma is None or sigma > self.last_sigma + 1e-6:
            self._finish_run()
            self.reset()
            self.start_sigma = sigma
            self.sigma_scale = self._detect_scale(args, sigma)
            self.last_sigma = sigma
            self.step = 0
            self.last_mode = "full"
            return {"img": self._full_step(args, kwargs, block_count, count=True)}

        if abs(sigma - self.last_sigma) <= 1e-6:
            if self.last_mode == "cache":
                return {"img": self._cache_step(args, kwargs, block_count, count=False)}
            return {"img": self._full_step(args, kwargs, block_count, count=False)}

        self.prev_sigma = self.last_sigma
        self.last_sigma = sigma
        self.step += 1
        sigma_n = sigma / self.sigma_scale
        prev_n = self.prev_sigma / self.sigma_scale
        pos = self._position(args, sigma_n)
        k = self._warm_blocks(block_count)
        in_window = self.start_percent <= pos <= self.end_percent
        slow = abs(prev_n - sigma_n) < self.threshold
        can_skip = (
            self.enabled
            and k < block_count
            and self.residual is not None
            and in_window
            and slow
            and self.consecutive_skips < self.mcs
        )
        if can_skip:
            self.last_mode = "cache"
            return {"img": self._cache_step(args, kwargs, block_count, count=True)}
        self.last_mode = "full"
        return {"img": self._full_step(args, kwargs, block_count, count=True)}

    def _warm_blocks(self, block_count):
        return max(0, min(block_count - 1, round(block_count * (1.0 - self.cache_depth))))

    def _resolve_cache_device(self, tensor):
        if self.resolved_cache_device is not None:
            return self.resolved_cache_device
        requested = self.cache_device
        if requested == "cpu":
            resolved = "cpu"
        elif requested == "gpu":
            resolved = tensor.device
        elif requested == "auto":
            resolved = "cpu"
            if tensor.is_cuda:
                try:
                    props = torch.cuda.get_device_properties(tensor.device)
                    if int(props.total_memory) >= self.AUTO_GPU_MIN_BYTES:
                        resolved = tensor.device
                except Exception:
                    resolved = tensor.device
        else:
            resolved = tensor.device
        self.resolved_cache_device = resolved
        print(f"TE-Speed-MiniMaxH3(OSS): cache device resolved {requested} -> {resolved}")
        return resolved

    def _store_residual(self, residual):
        target = self._resolve_cache_device(residual)
        if str(target) == "cpu":
            return residual.to("cpu")
        return residual

    def _full_step(self, args, kwargs, block_count, count=True):
        if count:
            self.full_steps += 1
            self.consecutive_skips = 0
            self.total_blocks += block_count
        original_block = kwargs["original_block"]
        h_in = None if self.snapshot is not None else args["img"].clone()
        h = original_block(args)["img"]
        residual = h - self.snapshot if self.snapshot is not None else h - h_in
        self.residual = self._store_residual(residual)
        if count and self.total_steps is not None and self.step >= self.total_steps:
            self._print_stats()
        return h

    def _cache_step(self, args, kwargs, block_count, count=True):
        if count:
            self.cache_hits += 1
            self.consecutive_skips += 1
            self.total_blocks += block_count
            self.skipped_blocks += block_count - self._warm_blocks(block_count)
        original_block = kwargs["original_block"]
        h = args["img"]
        k = self._warm_blocks(block_count)
        if k > 0:
            h = original_block({**args, "start": 0, "end": k})["img"]
        if self.residual is not None:
            h = h + self.residual.to(h.device)
        return h

    @staticmethod
    def _current_sigma(args):
        timestep = args["transformer_options"].get("sigmas")
        if timestep is None:
            return None
        return float(torch.as_tensor(timestep).flatten()[0].float())

    def _detect_scale(self, args, sigma):
        sample_sigmas = args["transformer_options"].get("sample_sigmas")
        if sample_sigmas is not None:
            ss0 = float(torch.as_tensor(sample_sigmas).flatten().float()[0])
            if abs(ss0) > 1e-9:
                s = sigma / ss0
                if abs(s) > 1e-9:
                    return s
        return 1.0

    def _position(self, args, sigma_n):
        sample_sigmas = args["transformer_options"].get("sample_sigmas")
        if sample_sigmas is not None:
            ss = torch.as_tensor(sample_sigmas).flatten().float()
            if ss.numel() > 1:
                self.total_steps = ss.numel() - 1
                idx = int((ss - sigma_n).abs().argmin())
                return min(1.0, max(0.0, idx / self.total_steps))
        if self.start_sigma is not None and self.start_sigma > 0.0:
            start_n = self.start_sigma / self.sigma_scale
            return min(1.0, max(0.0, (start_n - sigma_n) / start_n))
        return 1.0

    def _finish_run(self):
        if not self.printed and (self.full_steps + self.cache_hits) > 0:
            self._print_stats()

    def _print_stats(self):
        self.printed = True
        if self.total_blocks <= 0:
            return
        saved = 100.0 * self.skipped_blocks / self.total_blocks
        print(
            f"TE-Speed-MiniMaxH3(OSS): acceleration {saved:.1f}% "
            f"(full={self.full_steps} cache={self.cache_hits} of "
            f"{self.full_steps + self.cache_hits} steps, skipped "
            f"{self.skipped_blocks}/{self.total_blocks} blocks)"
        )


class _MiniMaxH3Snapshot:
    def __init__(self, cache):
        self.cache = cache

    def __call__(self, args, kwargs):
        self.cache.snapshot = args["img"].clone()
        return kwargs["original_block"](args)


class TESpeedMiniMaxH3:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "processing_control_value": ("FLOAT", {
                    "default": 0.12, "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "Sigma-delta threshold: smaller is safer; 0 disables caching."}),
                "processing_percent_1": ("FLOAT", {
                    "default": 0.1, "min": 0.0, "max": 0.49, "step": 0.01,
                    "tooltip": "Start of caching window; steps before it run fully."}),
                "processing_percent_2": ("FLOAT", {
                    "default": 0.9, "min": 0.51, "max": 1.0, "step": 0.01,
                    "tooltip": "End of caching window; steps after it run fully."}),
                "mcs": ("INT", {
                    "default": 2, "min": 0, "max": 10, "step": 1,
                    "tooltip": "Max consecutive cache steps before a forced full step; 0 disables caching."}),
                "device": (["auto", "cpu", "gpu"], {
                    "default": "auto",
                    "tooltip": "auto keeps residual on GPU at >=20 GiB VRAM, otherwise CPU; cpu saves VRAM but adds transfer overhead."}),
            },
            "optional": {
                "cache_depth": ("FLOAT", {
                    "default": 0.75, "min": 0.0, "max": 0.95, "step": 0.05,
                    "tooltip": "Fraction of trailing blocks served from cache. Higher is faster but may drift more."}),
            },
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "patch"
    CATEGORY = "sampling/custom_sampling/minimax_h3"

    def patch(self, model, processing_control_value, processing_percent_1,
              processing_percent_2, mcs, device, cache_depth=0.75):
        inner = self._find_minimax_dit(model)
        if inner is None:
            raise ValueError("TE-Speed-MiniMaxH3 only supports MiniMax H3 models")
        if not hasattr(inner, "_run_blocks"):
            print("TE-Speed-MiniMaxH3(OSS): MiniMax H3 core has no block_loop hooks; returning stock model.")
            return (model,)
        cache = _MiniMaxH3Cache(
            processing_control_value, processing_percent_1,
            processing_percent_2, mcs, device, cache_depth
        )
        model = model.clone()
        model.set_model_patch_replace(cache, "dit", "block_loop", 0)
        block_count = len(inner.blocks)
        k = cache._warm_blocks(block_count)
        if cache.enabled and 0 < k < block_count:
            model.set_model_patch_replace(_MiniMaxH3Snapshot(cache), "dit", "double_block", k)
        return (model,)

    @staticmethod
    def _find_minimax_dit(model):
        m = getattr(model, "model", None)
        seen = 0
        while m is not None and seen < 12:
            if type(m).__name__ == "MiniMaxH3Model":
                return m
            nxt = None
            for attr in ("model", "inner_model", "diffusion_model", "unet_model"):
                nxt = getattr(m, attr, None)
                if nxt is not None:
                    break
            m = nxt
            seen += 1
        return None
