"""检索与读取随项目分发的 EasyEDA API 文档。

MCP 工具 ``eda_api_search`` / ``eda_api_doc`` 基于本模块实现，让智能体无需
调用 grep、也无需自己遍历 ``references/``（400 多个文件）就能查到 API 签名。

检索范围必须覆盖全部文档目录：``references/``（类 / 枚举 / 接口 / 类型）、
``format/``（工程源文件格式）、``guide/``（开发指南）、``user-guide/``。
早期实现给文件数设了 400 的上限，结果 ``format/``、``guide/``、``user-guide/``
以及一部分 ``references/types/`` 根本没进索引——检索 "TMSchComponent"、
"PAD_NET" 这类只在格式文档里出现的名字会返回 0 命中，智能体就会误判成
"API 不存在"。现在改为全量索引，并保证任何一次检索都扫描全部文件。
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 允许通过 ``eda_api_doc`` 读取的目录（相对于项目根目录）。
DOC_ROOTS: tuple[str, ...] = ("references", "format", "guide", "user-guide")

#: 检索时优先扫描的入口文件。
INDEX_FILES: tuple[str, ...] = (
    "references/_quick-reference.md",
    "references/_index.md",
)

#: 单个文件最多返回多少条命中，避免一个文件把结果占满。
_MAX_HITS_PER_FILE = 3

_MAX_HIT_TEXT = 240

#: 检索词长度上限。超长 query 每次都要和 469 个文件名做一次长字符串比较，
#: 而且本来就不可能是有效关键词，直接拒绝。
MAX_QUERY_LENGTH = 512


class DocError(RuntimeError):
    """文件不存在，或路径落在文档目录之外。"""


@dataclass(slots=True)
class SearchHit:
    """一条检索结果。"""

    path: str
    line: int
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "text": self.text}


class DocIndex:
    """随项目分发的 markdown 文档的惰性索引（带缓存）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._lines: dict[str, list[str]] = {}
        self._raw: dict[str, str] = {}
        self._lower: dict[str, str] = {}
        self._files: list[str] | None = None

    # ─── 路径处理 ───────────────────────────────────────────────────────

    def resolve(self, relative: str) -> Path:
        """解析文档路径，拒绝任何落在文档目录之外的位置。"""
        if not relative or not relative.strip():
            raise DocError("path 不能为空")

        candidate = (self.root / relative.replace("\\", "/")).resolve()
        allowed = tuple((self.root / name).resolve() for name in DOC_ROOTS)
        if not candidate.is_relative_to(self.root) or not any(
            candidate == base or candidate.is_relative_to(base) for base in allowed
        ):
            raise DocError(
                f"{relative!r} 不在文档目录内（仅允许 {', '.join(DOC_ROOTS)}）"
            )
        if not candidate.is_file():
            raise DocError(f"文档文件不存在：{relative}")
        return candidate

    def rel(self, path: Path) -> str:
        """转换为相对项目根目录的 posix 路径。"""
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def _coerce_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
        """把外部传入的整数参数收敛到合法区间；非法输入抛 DocError。

        直接 ``int(value)`` 会让 ``limit="abc"`` 抛 ``ValueError`` 穿透工具层，
        MCP 客户端只会看到一个协议级错误，拿不到"参数不合法"的解释。
        """
        if isinstance(value, bool):
            raise DocError(f"{name} 必须是整数：{value!r}")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise DocError(f"{name} 必须是整数：{value!r}") from None
        return max(minimum, min(number, maximum))

    # ─── 文件枚举与缓存 ─────────────────────────────────────────────────

    def files(self) -> list[str]:
        """文档目录下所有 markdown 文件（相对路径，已排序）。"""
        if self._files is None:
            found: list[str] = []
            for name in DOC_ROOTS:
                base = self.root / name
                if not base.is_dir():
                    continue
                for path in sorted(base.rglob("*.md")):
                    if path.is_file():
                        found.append(self.rel(path))
            self._files = found
        return self._files

    def _raw_text(self, relative: str) -> str:
        """读取（并缓存）某个文档的原始文本。"""
        cached = self._raw.get(relative)
        if cached is None:
            try:
                cached = self.root.joinpath(relative).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                cached = ""
            self._raw[relative] = cached
        return cached

    def _lower_text(self, relative: str) -> str:
        """读取（并缓存）大小写折叠后的文本，用于快速包含判断。"""
        cached = self._lower.get(relative)
        if cached is None:
            cached = self._raw_text(relative).casefold()
            self._lower[relative] = cached
        return cached

    def _lines_of(self, relative: str) -> list[str]:
        """读取（并缓存）某个文档的所有行。"""
        cached = self._lines.get(relative)
        if cached is None:
            cached = self._raw_text(relative).splitlines()
            self._lines[relative] = cached
        return cached

    # ─── 检索 ───────────────────────────────────────────────────────────

    def search(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        """按文件名与文档内容做大小写不敏感检索。"""
        needle = (query or "").strip()
        if not needle:
            raise DocError("query 不能为空")
        if len(needle) > MAX_QUERY_LENGTH:
            raise DocError(
                f"query 过长（{len(needle)} 字符，上限 {MAX_QUERY_LENGTH}）；"
                "请用更精确的英文关键词"
            )
        lowered = needle.casefold()
        limit = self._coerce_int(limit, name="limit", minimum=1, maximum=50)

        files = self.files()

        # 1) 文件名命中（类名、接口名通常在这里）
        file_hits: list[dict[str, Any]] = []
        for relative in files:
            stem = Path(relative).stem.casefold()
            if lowered in stem or lowered in relative.casefold():
                file_hits.append({"path": relative})
                if len(file_hits) >= limit:
                    break

        # 2) 内容命中：先扫两个入口索引文件，再扫其余全部文件。
        #    关键点：**不能**因为"文件名命中过"就跳过内容兜底，也不能只扫
        #    前 N 个文件——format/guide 里有大量只在正文出现的字段名。
        ordered: list[str] = []
        seen: set[str] = set()
        for relative in INDEX_FILES:
            if relative in files and relative not in seen:
                ordered.append(relative)
                seen.add(relative)
        for relative in files:
            if relative not in seen:
                ordered.append(relative)
                seen.add(relative)

        hits: list[SearchHit] = []
        scanned = 0
        for relative in ordered:
            if len(hits) >= limit:
                break
            scanned += 1
            if lowered not in self._lower_text(relative):
                continue
            raw_lines = self._lines_of(relative)
            per_file = 0
            for index, text in enumerate(raw_lines, start=1):
                if lowered in text.casefold():
                    hits.append(
                        SearchHit(relative, index, text.strip()[:_MAX_HIT_TEXT])
                    )
                    per_file += 1
                    if per_file >= _MAX_HITS_PER_FILE or len(hits) >= limit:
                        break

        return {
            "query": needle,
            "files": file_hits,
            "matches": [hit.as_dict() for hit in hits],
            "indexedFiles": len(files),
            "scannedFiles": scanned,
            "note": (
                "检索已覆盖 references/、format/、guide/、user-guide/ 全部文件。"
                "用 eda_api_doc(path=...) 读取完整文档；建议先看 "
                "references/_index.md（模块总览）和 "
                "references/_quick-reference.md（全部方法签名）。"
            ),
        }

    # ─── 读取 ───────────────────────────────────────────────────────────

    def read(self, relative: str, *, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        """返回某个文档中一段带行号的窗口内容。"""
        path = self.resolve(relative)
        rel = self.rel(path)
        lines = self._lines_of(rel)
        total = len(lines)
        offset = self._coerce_int(offset, name="offset", minimum=0, maximum=len(lines))
        limit = self._coerce_int(limit, name="limit", minimum=1, maximum=2000)
        window = lines[offset : offset + limit]

        start = offset + 1 if window else 0
        end = offset + len(window)
        numbered = "\n".join(
            f"{start + index:>5}→ {text}" for index, text in enumerate(window)
        )
        return {
            "path": rel,
            "offset": offset,
            "returned": len(window),
            "totalLines": total,
            "hasMore": end < total,
            "text": numbered,
        }


def json_safe(value: Any) -> Any:
    """尽量把 EasyEDA 返回的任意值转换成可 JSON 序列化的形式。"""
    try:
        _json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value
