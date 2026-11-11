"""EasyEDA 枚举表（从 ``references/enums/*.md`` 解析）。

为什么需要这个模块
------------------
Run API Gateway 扩展是用下面的方式执行代码的::

    const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
    const fn = new AsyncFunction('eda', msg.code);
    const result = await fn(eda);

``new Function`` 的函数体只在**全局作用域**里求值，看不到扩展包模块作用域里的
枚举常量。实测（2026-11-11，嘉立创EDA 专业版 4.1.78 + 网关扩展）确认：

    typeof EPCB_LayerId        -> "undefined"   （抛 ReferenceError）
    typeof EDMT_EditorDocumentType -> "undefined"
    Object.keys(eda).length    -> 98，其中没有任何 "E*" 键
    globalThis 里也没有任何枚举名

也就是说：**文档里"用枚举成员、不要写裸数字"的写法，通过网关执行时必然抛错**。
而枚举值本身（``EPCB_LayerId.TOP = 1``、``BOARD_OUTLINE = 11``）在
``references/enums/*.md`` 里都有。

本模块把文档解析成表，并提供一段 JS 前导（prelude），由 ``eda_execute`` 按需注入，
让"用枚举写"的代码在网关里也能跑；同时提供 ``eda_enum`` 工具让智能体查到数值后
自己写裸数字（两条路都保留）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: 枚举文档所在目录（相对项目根目录）。
ENUM_DIR = "references/enums"

#: 枚举文档里成员表格的一行：``<tr><td>NAME</td><td>`VALUE`</td> ...``
_MEMBER_RE = re.compile(
    r"<tr><td>\s*([A-Za-z0-9_\\]+)\s*</td><td>\s*`([^`]*)`\s*</td>", re.S
)

#: 代码里可能引用枚举的形态（E 开头的常量名）。
_IDENT_RE = re.compile(r"\b(E[A-Z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b")

#: 用户自己在代码里声明过的名字，不能重复注入。
_DECL_RE = re.compile(
    r"\b(?:const|let|var|function|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)


def _coerce(raw: str) -> Any:
    """把文档里的取值文本转成 Python 值（数字/字符串）。"""
    text = raw.strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    quoted = re.fullmatch(r"'([^']*)'", text) or re.fullmatch(r'"([^"]*)"', text)
    if quoted:
        return quoted.group(1)
    # 兜底：原样当字符串，至少不会注入出语法错误
    return text


class EnumIndex:
    """``references/enums/*.md`` 的惰性解析表。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._members: dict[str, dict[str, Any]] = {}
        self._names: list[str] | None = None
        self._lower_names: dict[str, str] = {}

    # ─── 解析 ───────────────────────────────────────────────────────────

    def _enum_dir(self) -> Path:
        return self.root / ENUM_DIR

    def names(self) -> list[str]:
        """全部枚举名（按文件名排序）。"""
        if self._names is None:
            found: list[str] = []
            base = self._enum_dir()
            if base.is_dir():
                for path in sorted(base.glob("*.md")):
                    found.append(path.stem)
            self._names = found
            self._lower_names = {name.casefold(): name for name in found}
        return self._names

    def resolve(self, name: str) -> str | None:
        """枚举名（大小写不敏感）→ 规范名；不存在时返回 ``None``。"""
        self.names()
        if name in self._members or name in self.names():
            return name
        return self._lower_names.get(name.casefold())

    def members(self, name: str) -> dict[str, Any]:
        """某个枚举的全部成员与取值；不存在时返回空字典。"""
        canonical = self.resolve(name)
        if canonical is None:
            return {}
        cached = self._members.get(canonical)
        if cached is not None:
            return cached
        path = self._enum_dir() / f"{canonical}.md"
        result: dict[str, Any] = {}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for member, value in _MEMBER_RE.findall(text):
            result[member.replace("\\_", "_")] = _coerce(value)
        self._members[canonical] = result
        return result

    # ─── JS 生成 ────────────────────────────────────────────────────────

    def js_literal(self, name: str) -> str:
        """生成 ``Object.freeze({...})`` 形式的 JS 对象字面量。

        一律用 ``ensure_ascii=True``：枚举里确实存在中文取值
        （例如 ``SchToolBarDeviceName.FOUR_CHANNEL_OSCILLOSCOPE = '四通道示波器'``）。
        原样输出中文本身没问题，但 JS 字符串字面量里不能出现 U+2028/U+2029
        （它们是行终止符，会直接截断字符串、把注入的代码变成语法错误）。
        转成 ``\\uXXXX`` 后既保留原值，又不会踩到这个坑。
        """
        payload = self.members(name)
        body = ", ".join(
            f"{json.dumps(key)}: {json.dumps(value, ensure_ascii=True)}"
            for key, value in payload.items()
        )
        return f"Object.freeze({{{body}}})"

    def js_prelude(self, names: list[str]) -> str:
        """为若干枚举生成可直接前置到函数体里的 ``const`` 定义。"""
        chunks: list[str] = []
        for name in names:
            canonical = self.resolve(name)
            if canonical is None or not self.members(canonical):
                continue
            chunks.append(f"const {canonical} = {self.js_literal(canonical)};")
        if not chunks:
            return ""
        header = (
            "/* easyeda_mcp: 网关执行作用域里没有枚举符号，以下定义由 MCP 注入 */\n"
        )
        return header + "".join(chunks) + "\n"

    # ─── 代码分析 ───────────────────────────────────────────────────────

    def detect(self, code: str) -> list[str]:
        """找出代码里引用到的枚举名（排除代码自己声明的同名变量）。"""
        known = set(self.names())
        declared = set(_DECL_RE.findall(code))
        found: list[str] = []
        for ident in _IDENT_RE.findall(code):
            if ident in declared or ident in found:
                continue
            if ident in known:
                found.append(ident)
        return found

    def summary(self) -> dict[str, Any]:
        """统计信息，便于自检。"""
        names = self.names()
        return {
            "files": len(names),
            "members": sum(len(self.members(name)) for name in names),
        }
