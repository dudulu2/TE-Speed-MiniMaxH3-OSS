# MiniMaxH3 TE-Speed One-Click Safe Installer — V3

Windows 下为 **ComfyUI + MiniMax H3** 一键安装 / 一键安全卸载 `TE-Speed-MiniMaxH3-OSS`。

V3 的目标不是单纯“能装上”，而是：**兼容更多 ComfyUI 安装形式、支持 LoRA/Model patch 工作流、失败时自动清理本轮残留、卸载时不覆盖用户后来做的修改。**

## 一键使用

1. 下载本仓库 ZIP 并解压。
2. 双击 **`一键安装.bat`**。
3. 安装完成后完全关闭并重启 ComfyUI。
4. 需要撤销时双击 **`一键卸载.bat`**。
5. 可随时双击 **`检查状态.bat`** 做完整健康检查。

## V3 路径 / Python 识别

安装器现在接受：

- MiniMaxH3 整包根目录；
- 直接选择 ComfyUI 根目录；
- 常见 `C:` ~ 其他已挂载磁盘中的 MiniMaxH3 / ComfyUI；
- 找不到时弹出 Windows 文件夹选择器。

Python 会依次识别：

- `runtime\venv\Scripts\python.exe`；
- ComfyUI portable 的 `python_embeded\python.exe`；
- `python_embedded\python.exe`；
- 最后才尝试系统 Python。

## V3 LoRA / 复杂 MODEL 链支持

V2 只适合类似：

```text
UNETLoader ──► BasicScheduler
     └───────► BasicGuider
```

V3 不再要求最终 MODEL 必须直接来自 `UNETLoader`。只要 Scheduler 和 Guider 最终共享同一个 MODEL 输出，就会把 TE-Speed 插在**最终共同模型输出之后**：

```text
UNETLoader
    ↓
LoRA / ModelSampling / 其他 MODEL patch
    ↓
TESpeedMiniMaxH3
   ├────────► BasicScheduler.model
   └────────► BasicGuider.model
```

如果 Scheduler 与 Guider 已经变成真正不同的 MODEL 分支，V3 会跳过而不是猜测接线。

## 事务式安装与安全回退

安装前会先完成：

- custom node 文件冲突检查；
- MiniMax H3 `model.py` **无写入 preflight**；
- 只有核心结构可安全 patch 时才进入安装。

如果核心安装阶段失败，安装器会尝试只清理本轮已经写入的 installer-owned node / hook，而不是留下明显的半安装状态。

### model.py

- 不使用旧 `.bak` 整文件覆盖作为正常卸载方案；
- 给自己的 `_run_blocks` / `block_loop` 区域写唯一安全标记；
- 保存原 block loop 与 SHA256 状态；
- 卸载只删除自己的标记区域并恢复原 loop；
- 用户后来对其他部分的修改会保留；
- 如果 TE-Speed 自己的标记区域被改过，卸载停止而不是猜测覆盖；
- V3 继续识别 V2 safe markers，避免升级后失去安全卸载能力。

### workflow

- 支持顶层 workflow；
- 支持 `definitions.subgraphs`；
- 支持 LoRA / ModelSampling 等最终 MODEL producer；
- 新增节点包含 installer ownership / restore metadata；
- 卸载只删除 installer 创建的 TE 节点并恢复本安装器改变的 MODEL 边；
- 用户后来添加的 LoRA、后处理、提示词、分辨率和其他节点不会因整文件回退而消失；
- 如果用户主动修改了 TE-Speed 自己的接线，卸载停止。

### custom node

安装器保存 installer-owned 文件及 SHA256：

- 未修改 → 可安全删除；
- 用户改过 → 保留并报警；
- 不会无条件强删整个目录。

## 完整状态检查

`检查状态.bat` 现在检查三层：

1. MiniMax H3 `model.py` safe hook；
2. custom node manifest 与每个 installer-owned 文件 SHA256；
3. 已识别 MiniMax H3 workflow 中 TE-Speed 是否存在、是否为 installer-owned。

因此不再出现“model hook 在，但节点文件已经坏了仍显示正常”的情况。

## TE-Speed 参数

| 参数 | 默认值 | 说明 |
| --- | ---: | --- |
| `processing_control_value` | `0.12` | sigma 差阈值；0 关闭缓存 |
| `processing_percent_1` | `0.1` | 缓存窗口开始 |
| `processing_percent_2` | `0.9` | 缓存窗口结束 |
| `mcs` | `2` | 最大连续缓存步 |
| `device` | `auto` | 自动选择缓存残差位置 |
| `cache_depth` | `0.75` | 缓存尾部 block 比例 |

### `device=auto`

V3 默认：

- CUDA 显存 **≥ 20 GiB**：残差保留在 GPU，避免 PCIe 往返；
- 显存较小：残差放 CPU，优先降低 VRAM 压力；
- 仍可手动强制 `cpu` 或 `gpu`。

因此 24GB / 32GB / 更大显存卡默认偏速度，12GB / 16GB 卡默认偏稳妥显存占用。

## 关于“45% 加速”

节点会打印缓存命中、完整计算步和跳过 block 比例，但：

**跳过 block 的百分比不等于 wall-clock 实测加速百分比。**

实际速度取决于 GPU、分辨率、帧数、采样步数、offload、CPU/GPU 缓存位置和工作流。约 45% 仍应视作参考目标，而不是所有设备保证值。

## 当前兼容策略

`patch_model.py` 对 MiniMax H3 官方 block loop 使用严格锚点。ComfyUI 后续如果改变核心结构：

- `--preflight` 会失败；
- 一键安装会停止；
- 不会强行套用未知版本 patch。

## 测试

```bash
python -m unittest -v tests/test_safety.py
```

覆盖：

- model patch 无写入 preflight；
- model patch install/revert round-trip；
- model.py 后续无关修改保留；
- installer-owned patch 被修改后拒绝危险回退；
- 顶层 workflow；
- LoRA 作为最终 MODEL producer；
- Scheduler / Guider 分叉时拒绝猜测；
- workflow 后续无关编辑保留；
- TE-Speed 自身接线被修改后拒绝回退。

## 分支说明

V3 开发首先位于：

```text
v3-installer-hardening
```

在完成验证前不影响当前 `main`。

## License

保留 LGPL-3.0 License 与相关版权声明，见 `LICENSE`。
