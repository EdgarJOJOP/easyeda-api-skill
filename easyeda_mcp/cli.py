"""命令行入口。

用法示例
--------
以 stdio 方式运行 MCP 服务（MCP 客户端会自动这样启动它）::

    python -m easyeda_mcp

改用 Streamable HTTP 方式运行::

    python -m easyeda_mcp --transport streamable-http --http-port 8848

只运行桥接服务、不启动 MCP 传输层（方便用 curl 调试）::

    python -m easyeda_mcp --transport none

把工具接到一个已经在运行的桥接服务上::

    python -m easyeda_mcp --bridge off
"""

import argparse
import asyncio
import logging
import sys

from . import __version__
from . import protocol as P
from .bridge import BridgeServer
from .client import BridgeClient
from .server import build_server

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

_BANNER_WIDTH = 66


def _banner(title: str, rows: list[tuple[str, str]]) -> None:
    """在 stderr 上打印启动横幅（stdout 属于 MCP 协议通道，不能占用）。

    刻意不画左右竖线：中文字符在终端里占两列，画框容易错位。
    """
    line = "=" * _BANNER_WIDTH
    print(line, file=sys.stderr)
    print(title, file=sys.stderr)
    print(line, file=sys.stderr)
    for label, value in rows:
        print(f"  {label}：{value}", file=sys.stderr)
    print(line, file=sys.stderr)


def _configure_logging(level: str) -> None:
    """所有日志一律写 stderr；stdout 留给 MCP 传输层。"""
    # 终端编码可能无法表示某些字符（例如 EDA 返回的特殊符号），
    # 用 replace 兜底，避免写日志时抛 UnicodeEncodeError 直接打崩 MCP 进程。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 特殊流不支持时跳过
                pass
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
    )
    # aiohttp 的访问日志对本机桥接没有意义，只会有噪音
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器（帮助文本使用中文）。"""
    parser = argparse.ArgumentParser(
        prog="easyeda-mcp",
        description=(
            "EasyEDA 专业版 MCP 服务 + WebSocket 桥接服务。"
            f"EasyEDA 专业版客户端（run-api-gateway 扩展）通过端口 "
            f"{P.PORT_START}-{P.PORT_END} 连接。"
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "none"),
        default="stdio",
        help=(
            "MCP 传输方式：stdio（默认，MCP 客户端使用）、streamable-http，"
            "或 none（只运行 WebSocket 桥接服务）。"
        ),
    )
    parser.add_argument(
        "--bridge",
        choices=("auto", "on", "off"),
        default="auto",
        help=(
            "auto：复用已在运行的桥接服务，没有则启动一个（默认）；"
            "on：始终在本进程内启动桥接服务；off：只连接已有的桥接服务。"
        ),
    )
    parser.add_argument(
        "--host",
        default=P.DEFAULT_HOST,
        help=f"桥接服务绑定的本机地址（默认：{P.DEFAULT_HOST}）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"直接绑定指定端口，而不是扫描 {P.PORT_START}-{P.PORT_END}",
    )
    parser.add_argument(
        "--http-host",
        default=P.DEFAULT_HOST,
        help="streamable-http 传输方式监听的地址（默认：127.0.0.1）",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=8848,
        help="streamable-http 传输方式监听的端口（默认：8848）",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="stderr 上的日志级别（默认：INFO）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"easyeda-mcp {__version__}",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    """按命令行参数启动桥接服务与 MCP 服务。"""
    bridge: BridgeServer | None = None
    existing: BridgeClient | None = None

    if args.bridge in ("auto", "off"):
        existing = await BridgeClient.discover(host=args.host)
        if args.bridge == "off" and existing is None:
            raise SystemExit(
                f"在端口 {P.PORT_START}-{P.PORT_END} 上没有发现 EasyEDA 桥接服务。"
                "请先用 `python -m easyeda_mcp` 启动一个。"
            )

    if args.bridge == "off" and existing is not None and args.port is not None:
        # `--bridge off` 的语义是"只连已有实例"，此时 `--port` 没有意义。
        # 早期实现会让这个组合去新建一个桥接，等于违背了 off 的约定，
        # 还可能在指定的端口上留下一个多余的监听。
        print(
            f"--bridge off：忽略 --port {args.port}，复用已在运行的桥接实例（端口 {existing.port}）。",
            file=sys.stderr,
        )
        args.port = None

    if existing is not None and args.port is None:
        client = existing
        _banner(
            "EasyEDA MCP 桥接服务（Python）",
            [
                ("桥接地址", f"复用已在运行的实例，端口 {existing.port}"),
                ("传输方式", args.transport),
                ("服务标识", P.SERVICE_ID),
            ],
        )
    else:
        port_start = args.port if args.port is not None else P.PORT_START
        port_end = args.port if args.port is not None else P.PORT_END
        bridge = BridgeServer(host=args.host, port_start=port_start, port_end=port_end)
        port = await bridge.start()
        client = BridgeClient(host=args.host, port=port)
        _banner(
            "EasyEDA MCP 桥接服务（Python）",
            [
                ("桥接地址", f"http://{args.host}:{port}"),
                ("EDA 地址", f"ws://{args.host}:{port}/eda"),
                (
                    "端口范围",
                    str(port_start) if port_start == port_end else f"{port_start}-{port_end}",
                ),
                ("服务标识", P.SERVICE_ID),
                ("传输方式", args.transport),
            ],
        )
        print(
            "正在等待 EasyEDA 专业版连接（扩展 run-api-gateway.eext）……",
            file=sys.stderr,
        )

    mcp = build_server(client=client, log_level=args.log_level)
    try:
        if args.transport == "stdio":
            await mcp.run_stdio_async()
        elif args.transport == "streamable-http":
            print(
                f"MCP 端点：http://{args.http_host}:{args.http_port}/mcp",
                file=sys.stderr,
            )
            await mcp.run_streamable_http_async(
                host=args.http_host, port=args.http_port, streamable_http_path="/mcp"
            )
        else:
            # 仅桥接模式：保持事件循环存活，直到被中断
            while True:
                await asyncio.sleep(3600)
    finally:
        if bridge is not None:
            await bridge.stop()


def main(argv: list[str] | None = None) -> int:
    """进程入口，返回退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("已被中断，正在退出。", file=sys.stderr)
        return 0
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - 启动阶段的致命错误
        print(f"错误：{exc}", file=sys.stderr)
        logging.getLogger(__name__).debug("致命错误", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
