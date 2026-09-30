# -*- coding: utf-8 -*-
"""Houdini AI 工具集。

不走 MCP：会话在 Houdini 里，AI 在后台线程干活，只向网关发出站 HTTPS，
不监听任何端口。

模块分工：
    config   网关配置（复用 Claude Code 的 settings.json）
    gateway  Anthropic Messages API 客户端
    catalog  节点类型目录 + 参数真相（防幻觉地基）
    schema   给模型看的工具定义
    builder  批量建网 + 硬校验 + 容器边界强制
    session  每个节点的对话状态与快照
    runner   后台工作线程 + 主线程调度
    node     AI 节点的创建、识别、状态显示
    prompt   系统提示词
"""

__all__ = [
    "config",
    "gateway",
    "catalog",
    "schema",
    "builder",
    "session",
    "runner",
    "node",
    "prompt",
]
