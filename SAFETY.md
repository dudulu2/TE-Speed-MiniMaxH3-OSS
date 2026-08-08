# Safety model

本项目的卸载目标是：**只撤销自己做过且仍能确定身份的修改。**

- `model.py`：标记区域 + 原 block loop 状态恢复，不整文件回滚。
- workflow：节点内嵌恢复元数据，仅反向恢复安装器创建的 3 条 link 变化。
- custom node：按 manifest + SHA256 删除，用户修改文件保留。
- 任意关键状态不匹配：返回非 0，保留现状，不做猜测性覆盖。

恢复副本位于目标 ComfyUI：

```text
ComfyUI\.te_speed_minimaxh3\recovery\
ComfyUI\user\default\workflows\.tespeed_recovery\
```

它们是人工救援用，不是“一键卸载”的默认恢复来源。
