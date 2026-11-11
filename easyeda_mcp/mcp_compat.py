"""MCP Python SDK 兼容层。

本项目原本只支持官方 ``mcp>=2.0``（``mcp.server.mcpserver.MCPServer``），
但现场常用的解释器（例如 ``D:/env/mcp/python.exe``）里装的是 1.x
（``mcp.server.fastmcp.FastMCP``）。两者在本项目用到的接口上几乎是同一套：
``tool()`` / ``resource()`` / ``run_stdio_async()`` / ``run_streamable_http_async()``。

这里做一层最小适配，让同一份代码在 1.x 与 2.x 上都能启动，避免"换一个
解释器就 ModuleNotFoundError"这种与 EDA 能力无关的失败。
"""

from __future__ import annotations

from typing import Any

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer  # type: ignore[import-not-found]

    MCP_SDK = "2.x"
    SERVER_CLASS = "mcp.server.mcpserver.MCPServer"
except ImportError:  # pragma: no cover - 取决于安装的 SDK 版本
    from mcp.server.fastmcp import FastMCP

    MCP_SDK = "1.x"
    SERVER_CLASS = "mcp.server.fastmcp.FastMCP"

    class MCPServer(FastMCP):  # type: ignore[no-redef]
        """把 2.x 的构造参数映射到 1.x 的 ``FastMCP``。

        1.x 的构造函数没有 ``title`` / ``description`` / ``version``，但这些
        字段只影响客户端展示，不影响任何工具行为，丢弃即可。
        """

        def __init__(
            self,
            name: str | None = None,
            title: str | None = None,
            description: str | None = None,
            instructions: str | None = None,
            version: str = "",
            **kwargs: Any,
        ) -> None:
            self.title = title
            self.description = description
            self.version = version
            super().__init__(name=name, instructions=instructions, **kwargs)


__all__ = ["MCPServer", "MCP_SDK", "SERVER_CLASS"]
