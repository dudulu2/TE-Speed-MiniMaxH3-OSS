# MiniMaxH3 TE-Speed One-Click Safe Installer

Windows 下为 **ComfyUI + MiniMax H3** 一键安装 / 一键安全卸载 `TE-Speed-MiniMaxH3-OSS`。

本项目重点不是“能装上”而是**出问题时不覆盖用户后来更新的 ComfyUI 或工作流**。

## 一键使用

1. 下载本仓库 ZIP 并解压。
2. 双击 **`一键安装.bat`**。
3. 安装完成后重启 ComfyUI。
4. 需要撤销时双击 **`一键卸载.bat`**。

安装器会自动寻找：

- 安装包位于 MiniMaxH3 根目录内的情况；
- `C:\MiniMaxH3` ~ `H:\MiniMaxH3`；
- 找不到时允许手动输入 MiniMaxH3 根目录。

要求目标环境包含：

```text
<MiniMaxH3>\
├─ ComfyUI\
│  └─ comfy\ldm\minimax\model.py
└─ runtime\venv\Scripts\python.exe
```

## 安全回退设计

### 1. 不再用旧 `.bak` 整文件覆盖 `model.py`

安装时会：

- 验证当前 MiniMax H3 `model.py` 的 stock block loop 是否匹配；
- 给注入的两段代码加唯一安全标记；
- 保存原 block loop 和 SHA256 状态；
- 额外保存恢复副本，仅作为人工救援用途。

卸载时只删除本安装器带标记的两段修改并恢复原 block loop。

如果安装后用户更新了 `model.py` 的其他部分，**这些后来修改会保留**。

如果用户或其他插件直接修改了 TE-Speed 自己的标记区域，卸载会**停止而不是猜测覆盖**。

### 2. 工作流采用“手术式撤销”

安装器支持：

- 顶层 MiniMax H3 workflow；
- `definitions.subgraphs` 中封装的 MiniMax H3 workflow。

安装时将：

```text
UNETLoader.MODEL
      │
      ▼
TESpeedMiniMaxH3
   ├────────► BasicScheduler.model
   └────────► BasicGuider.model
```

并在新增节点中记录恢复元数据。

卸载时只：

- 删除由本安装器创建的 `TESpeedMiniMaxH3` 节点；
- 恢复原来的两条 MODEL 连接；
- 保留用户后来新增的 LoRA、后处理、提示词、分辨率、节点和其他工作流改动。

如果用户后来主动改过 TE-Speed 的连接关系，卸载会停止，**不会恢复整个旧 workflow 覆盖用户数据**。

### 3. Custom Node 不再整目录强删

安装器会记录自己写入的文件及 SHA256。

卸载时：

- 文件仍与安装版本一致 → 删除；
- 文件被用户修改过 → 保留并报警；
- 不会无条件删除整个目录。

## 安装过程中发生错误时

安全原则：**宁可停止，也不做不确定覆盖**。

常见提示：

- `legacy TE-Speed patch detected`：检测到旧版/未追踪的 TE-Speed patch，新版不会直接覆盖；
- `expected stock ... not found`：当前 ComfyUI 结构与已验证版本不同，安装停止；
- `workflow ... refusing to guess`：工作流有多条可能的采样链，自动注入跳过；
- `TE-Speed wiring was changed after install`：卸载时发现用户后来改过 TE-Speed 接线，停止卸载并保留节点/model patch，避免工作流损坏。

## 状态检查

双击：

```text
检查状态.bat
```

可以检查 model hook 与工作流中的安全 TE-Speed 节点。

## 当前兼容性

本项目的 `model.py` patch 逻辑已按 2026-08-08 的 ComfyUI MiniMax H3 实现校验：当前官方代码仍使用 `patches_replace / double_block` block loop，因此安装器只在该结构精确匹配时才修改。

未来 ComfyUI 如果改变 MiniMax H3 的内部 block loop，安装器会拒绝修改，而不是强行套补丁。

## TE-Speed 参数

| 参数 | 默认值 | 说明 |
| --- | ---: | --- |
| `processing_control_value` | `0.12` | sigma 差阈值；0 关闭缓存 |
| `processing_percent_1` | `0.1` | 缓存窗口开始 |
| `processing_percent_2` | `0.9` | 缓存窗口结束 |
| `mcs` | `2` | 最大连续缓存步 |
| `device` | workflow 自动注入为 `cpu` | 残差缓存位置 |
| `cache_depth` | `0.75` | 缓存尾部 block 比例 |

节点原始说明中的参考数据为约 **45% wall-clock 加速**；实际速度与显卡、分辨率、步数、工作流和参数有关。

## 文件结构

```text
MiniMaxH3-TE-Speed-OneClick/
├─ 一键安装.bat
├─ 一键卸载.bat
├─ 检查状态.bat
├─ installer.py
├─ README.md
├─ LICENSE
├─ tests/
│  └─ test_safety.py
└─ TE-Speed-MiniMaxH3-OSS/
   ├─ __init__.py
   ├─ nodes.py
   ├─ patch_model.py
   ├─ tespeed_workflow_patch.py
   └─ Example_Workflow.json
```

## 测试

```bash
python -m unittest -v tests/test_safety.py
```

覆盖：

- model patch 安装/卸载 round-trip；
- 安装后 `model.py` 发生无关修改时仍能保留；
- TE-Speed 自己的 patch 区域被改动时拒绝危险回退；
- 顶层 workflow 自动注入；
- workflow 后续无关编辑保留；
- TE-Speed 接线被人为修改后拒绝猜测回退。

## License

保留原项目所附 LGPL-3.0 License 与版权声明，见 `LICENSE`。
