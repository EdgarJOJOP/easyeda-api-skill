"""EasyEDA 专业版 MCP 服务与 WebSocket/HTTP 桥接（Python 3.10+）。

基于官方 Python ``mcp`` SDK 对 `easyeda-api-skill
<https://github.com/easyeda/easyeda-api-skill>`_ 的重写实现。
EasyEDA 专业版的扩展（``run-api-gateway.eext``）仍然通过完全相同的
WebSocket 协议与端口范围连接，而 AI 侧从直接发 HTTP 改为使用 MCP 工具。

MCP SDK 1.x（``mcp.server.fastmcp.FastMCP``）与 2.x
（``mcp.server.mcpserver.MCPServer``）都受支持，见 :mod:`easyeda_mcp.mcp_compat`。
"""

__version__ = "2.1.0"
__all__ = ["__version__"]
