"""桥接服务与客户端抛出的异常类型。"""

from __future__ import annotations

from typing import Any


class BridgeError(RuntimeError):
    """桥接 / 进程间通信失败的基类。

    ``status_code`` 表示同样的失败通过 ``POST /execute`` 上报时使用的 HTTP
    状态码，与 Node.js 版本保持一致（"未连接" 用 503，其余用 500），
    这样已有的外部脚本无需改动。
    """

    status_code = 500

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        #: 桥接服务返回的原始字段（超时标记、requestId 等），便于上层无损转发。
        self.details: dict[str, Any] = dict(details or {})


class EdaNotConnectedError(BridgeError):
    """没有任何 EasyEDA 窗口可用（或选中的窗口已经断开）。

    ``delivered`` 是本类最关键的信息：

    * ``False`` —— 请求**从未**送达 EasyEDA（没有窗口、窗口已断、发送失败）。
      此时重试是安全的：图纸上不可能已经发生了改动。
    * ``True`` —— 请求已经送达 EasyEDA，之后连接才断开。EasyEDA 里可能已经
      执行了这段代码（例如创建了器件、画了线），自动重试会造成重复器件。

    因此上层只允许对 ``delivered=False`` 的失败做自动重试。
    """

    status_code = 503

    def __init__(
        self,
        message: str,
        *,
        delivered: bool = False,
        available: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.delivered = delivered
        self.available: list[str] = list(available or [])

    def to_payload(self) -> dict[str, Any]:
        """把连接信息展开成响应体字段（HTTP / MCP 共用）。"""
        return {
            "delivered": self.delivered,
            "availableWindows": list(self.available),
        }


class EdaTimeoutError(BridgeError):
    """EasyEDA 窗口收到了请求，但在超时时间内没有返回结果。

    ⚠️ **超时 != 失败**。这是本桥接最容易误判的一点，务必区分：

    * 请求已经通过 WebSocket 送达 EasyEDA，扩展也在主线程里继续执行它；
    * 只是本地 ``asyncio.wait_for`` 不再等待了，所以拿不到 ``return`` 的值；
    * 典型的"慢"接口：``pcb_Document.autoRouting``（布 20 个以上网络要
      60~300 秒）、``pcb_Document.autoLayout``、Gerber/BOM 导出；
    * 之前把这种超时当成"布线器返回 undefined / 差分对无解"，导致反复
      重提同一个布线任务、把已经布好的成果反复清空。

    调用方的正确做法：

    1. **不要立刻重试同一个任务**（会造成重复布线 / 重复放置器件 / 反复
       clearRouting）。全局布线类接口每次调用都会先清空再用 ``isKeep``
       重布，重复提交等于把进度扔掉；
    2. 用**轻量的只读查询**轮询进度，例如
       ``await eda.pcb_PrimitiveLine.getAllPrimitiveId()``（0~1ms），
       **不要**用 ``pcb_PrimitiveLine.getAll()``（千条走线时本身要几十秒，
       会把查询也拖超时）；
    3. 需要拿返回值时，调大 ``timeout_seconds`` 重新提交。
    """

    status_code = 500

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        timeout_s: float | None = None,
        still_running: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.request_id = request_id
        self.timeout_s = timeout_s
        self.still_running = still_running

    def to_payload(self) -> dict[str, Any]:
        """把超时信息展开成可直接塞进响应体的字段。"""
        return {
            "timeout": True,
            "stillRunning": self.still_running,
            "requestId": self.request_id,
            "timeoutSeconds": self.timeout_s,
            "hint": (
                "超时不代表失败：请求已送达 EasyEDA 并在其内部继续执行。"
                "请勿重复提交同一任务；改用轻量只读查询轮询进度"
                "（await eda.pcb_PrimitiveLine.getAllPrimitiveId()），"
                "或用更大的 timeout_seconds 重新提交。"
            ),
        }
