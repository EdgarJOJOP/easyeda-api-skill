"""EasyEDA 桥接的通信协议定义（WebSocket / HTTP 上的 JSON 报文）。

协议与上游 Node.js 版本（easyeda-api-skill 的 ``scripts/bridge-server.mjs``）
完全保持字节级兼容，因此 EasyEDA 专业版的扩展 ``run-api-gateway.eext``
无需任何改动即可继续使用：

  ┌──────────────┐   HTTP/WS     ┌────────────────┐   WebSocket    ┌──────────┐
  │   AI 智能体   │ ◄───────────► │    桥接服务     │ ◄───────────► │ EasyEDA  │
  │  (MCP 工具)  │   端口范围     │   (Python)     │   端口范围      │ (客户端) │
  └──────────────┘  49620-49629  └────────────────┘  49620-49629   └──────────┘

报文格式::

    {"type": "execute" | "result" | "error" | "ping" | "pong" | "handshake" | "register",
     "id": "<请求 uuid>",
     "code": "<JavaScript 源码>",
     "result": <任意值>,
     "error": "<错误信息>",
     "service": "easyeda-bridge",
     "clientType": "eda" | "agent",
     "windowId": "<EDA 窗口 ID>",
     "timestamp": <Unix 毫秒>}

本模块不启动任何服务、也不建立任何网络连接。
"""

import time
import uuid
from typing import Any

#: 握手 / `/health` 的服务标识。客户端必须校验该字段。
SERVICE_ID = "easyeda-bridge"

#: 只绑定本机回环地址。
DEFAULT_HOST = "127.0.0.1"

#: AI 侧与 EasyEDA 扩展共同扫描的端口范围。
PORT_START = 49620
PORT_END = 49629

#: 单次请求的默认超时时间（对应 Node.js 版的 REQUEST_TIMEOUT_MS = 30_000）。
#:
#: ⚠️ 超时只表示"本地不再等待"：请求已经送达 EasyEDA，它内部仍在继续执行。
#: 因此调用方不能把 ``EdaTimeoutError`` 当成操作失败并重复提交同一个任务。
#: 长任务（``autoRouting`` 布几十个网络、``autoLayout``、Gerber/BOM 导出）
#: 需要 1~5 分钟，必须显式传 ``timeout``（``POST /execute`` 的 body 字段
#: 或 MCP 工具的 ``timeout_seconds``），否则必然"看起来失败"。
DEFAULT_REQUEST_TIMEOUT = 30.0

#: 长任务（布线 / 自动布局 / 制造文件导出）建议使用的超时时间。
LONG_REQUEST_TIMEOUT = 300.0

#: ``POST /execute`` 允许客户端通过 body 中的 ``timeout`` 字段覆盖默认值；
#: 为防滥用设一个上限。取 30 分钟，与嘉立创EDA 官方 CLI 的单次调用预算
#: （``easyeda-pro invoke --timeout`` 上限 1800000ms）对齐：大型工程的整体
#: 自动布线、Gerber/ODB++ 导出确实会跑过 10 分钟。
MAX_REQUEST_TIMEOUT = 1800.0

# 报文类型常量
MSG_EXECUTE = "execute"
MSG_RESULT = "result"
MSG_ERROR = "error"
MSG_PING = "ping"
MSG_PONG = "pong"
MSG_HANDSHAKE = "handshake"
MSG_REGISTER = "register"

#: EDA 连接尚未注册窗口时使用的占位窗口 ID。
UNREGISTERED_WINDOW_ID = "unregistered"


def now_ms() -> int:
    """当前 Unix 时间戳（毫秒），与 Node.js 版保持一致。"""
    return int(time.time() * 1000)


def new_request_id() -> str:
    """生成新的请求 ID，用于把 ``execute`` 与 ``result``/``error`` 配对。"""
    return str(uuid.uuid4())


def handshake_message(client_type: str) -> dict[str, Any]:
    """WebSocket 升级成功后，服务端立即发给客户端的握手报文。"""
    return {
        "type": MSG_HANDSHAKE,
        "service": SERVICE_ID,
        "clientType": client_type,
        "timestamp": now_ms(),
    }


def execute_message(request_id: str, code: str, window_id: str) -> dict[str, Any]:
    """发给某个 EasyEDA 窗口的 ``execute`` 指令。"""
    return {
        "type": MSG_EXECUTE,
        "id": request_id,
        "code": code,
        "windowId": window_id,
        "timestamp": now_ms(),
    }


def pong_message(request_id: Any) -> dict[str, Any]:
    """回复 EDA 客户端发来的 ``ping``。"""
    return {"type": MSG_PONG, "id": request_id, "timestamp": now_ms()}


def result_message(request_id: Any, result: Any) -> dict[str, Any]:
    """面向 agent 客户端的一次成功执行结果。"""
    return {"type": MSG_RESULT, "id": request_id, "result": result, "timestamp": now_ms()}


def error_message(request_id: Any, error: str) -> dict[str, Any]:
    """面向 agent 客户端的一次失败结果。"""
    return {"type": MSG_ERROR, "id": request_id, "error": error, "timestamp": now_ms()}


def is_bridge_health(payload: Any) -> bool:
    """判断任意 JSON 载荷是否就是本桥接服务的 ``/health`` 响应。"""
    return isinstance(payload, dict) and payload.get("service") == SERVICE_ID
