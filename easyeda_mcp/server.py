"""组装 :class:`mcp.server.mcpserver.MCPServer` 实例。

工具定义在 :mod:`easyeda_mcp.tools`；本模块只负责把它们、文档资源以及
交给智能体的系统级说明（instructions）拼装到一起。
"""

import logging
from pathlib import Path

from . import __version__
from .client import BridgeClient
from .docs import DocIndex
from .mcp_compat import MCP_SDK, MCPServer, SERVER_CLASS
from .tools import register_tools

logger = logging.getLogger(__name__)

#: 项目根目录：包含 SKILL.md、references/、format/、guide/ 的那一层。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

INSTRUCTIONS = """\
嘉立创EDA（EasyEDA 专业版）控制桥。本 MCP 服务通过本机 WebSocket 桥接服务
与运行中的 EasyEDA 专业版客户端通信。

工作流程
1. `eda_status` —— 先确认桥接服务在运行、且至少有一个 EasyEDA 窗口已连接。
   任何传输错误之后，也要重新调用它确认状态。
2. `eda_api_search` / `eda_api_doc` —— 写代码之前先查清接口签名、枚举成员与
   参数类型，不要凭记忆猜测。
3. `eda_execute` —— 在 EasyEDA 中执行 JavaScript 并读取返回值；需要连续执行
   多段时用 `eda_execute_batch` 一次完成；只想确认窗口是否在线时用 `eda_ping`。
4. `eda_list_windows` / `eda_select_window` —— 连接了多个 EasyEDA 窗口时，
   指定代码发往哪一个窗口。

代码执行环境
传入的代码会被包装成如下形式在 EasyEDA 专业版中执行：

    async function (eda) { /* 你的代码 */ }

- `eda` 是 API 根对象（`eda.dmt_Project`、`eda.pcb_PrimitiveLine` 等）。
- 必须用 `return` 返回想要的结果；`console.log` 的输出不会被捕获。
- 凡是返回 `Promise<...>` 的接口（绝大多数）都必须 `await`。
- 使用枚举成员（例如 `EPCB_LayerId.TOP`），不要用裸数字。
- PCB 坐标单位是 1 mil，原理图坐标单位是 0.01 英寸（10 mil）。
- 调用 PCB_*/SCH_* 接口前先确认当前文档类型
  （`eda.dmt_SelectControl.getCurrentDocumentInfo()`）。
- 扩展主进程中不能使用 `window`、`document`、`localStorage` 等浏览器全局对象，
  请改用对应的 `eda.sys_*` 接口。

文档
`references/_index.md` 是模块与类的总索引，`references/_quick-reference.md`
收录了全部方法签名，`format/` 描述工程文件的源格式，`guide/` 涵盖扩展开发。
需要时用 `eda_api_doc` 读取。
"""


def build_server(
    *,
    client: BridgeClient,
    project_root: Path | None = None,
    log_level: str = "INFO",
) -> MCPServer:
    """创建 MCP 服务实例，并注册全部 EasyEDA 工具与文档资源。"""
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    index = DocIndex(root)

    mcp: MCPServer = MCPServer(
        name="easyeda",
        title="EasyEDA Pro",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level=log_level,
    )
    register_tools(mcp, client, index)
    _register_resources(mcp, root)
    logger.debug(
        "MCP 服务已构建（项目根目录：%s，MCP SDK：%s / %s）", root, MCP_SDK, SERVER_CLASS
    )
    return mcp


def _read_text(path: Path) -> str:
    """读取文档文本；文件缺失时返回一段可读的提示而不是抛异常。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - 仅在文档缺失时触发
        return f"# 不可用\n\n无法读取 {path.name}：{exc}\n"


def _register_resources(mcp: MCPServer, root: Path) -> None:
    """把主要的文档入口注册为 MCP 资源。"""

    @mcp.resource(
        "easyeda://skill",
        name="SKILL.md",
        title="EasyEDA API 技能说明",
        description="完整技能文档：环境准备、工作流程、代码范式与常见错误。",
        mime_type="text/markdown",
    )
    def skill_resource() -> str:
        return _read_text(root / "SKILL.md")

    @mcp.resource(
        "easyeda://references/index",
        name="references/_index.md",
        title="EasyEDA API 总索引",
        description="EasyEDA API 全部类、枚举与接口的总索引。",
        mime_type="text/markdown",
    )
    def references_index_resource() -> str:
        return _read_text(root / "references" / "_index.md")

    @mcp.resource(
        "easyeda://references/quick-reference",
        name="references/_quick-reference.md",
        title="EasyEDA API 速查表",
        description="一份文档收录 EasyEDA API 的全部方法签名。",
        mime_type="text/markdown",
    )
    def quick_reference_resource() -> str:
        return _read_text(root / "references" / "_quick-reference.md")
