"""不依赖当前工作目录的 MCP 启动脚本。

MCP 客户端的 ``command`` 字段必须是可执行文件的路径，不能写
``cd D:/eda && python -m easyeda_mcp`` 这类 shell 组合（客户端不会经过
shell 解释，会把整串内容当成文件名，从而报 "file does not exist"）。

如果客户端不支持设置 ``cwd``，直接用本脚本作为入口即可::

    command = "D:\\env\\eda\\python.exe"
    args    = ["D:\\eda\\scripts\\run_mcp.py"]

命令行参数会原样透传给 :mod:`easyeda_mcp.cli`，例如::

    python D:/eda/scripts/run_mcp.py --transport streamable-http
"""

import sys
from pathlib import Path

# 把项目根目录（包含 easyeda_mcp/ 的那一层）加入模块搜索路径，
# 这样无论从哪个目录启动，`import easyeda_mcp` 都能成功。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from easyeda_mcp.cli import main  # noqa: E402  （必须在 sys.path 调整之后导入）

if __name__ == "__main__":
    raise SystemExit(main())
