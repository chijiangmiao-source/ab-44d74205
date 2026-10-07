"""公式词法/语法分析。

支持：有符号 64 位整数字面量、单元格引用（A1..H16）、加/减/乘/整除、
括号、一元正负号以及条件式 if(条件, 真值, 假值)。
整除 `/` 为向零取整的 64 位整数除法（用于覆盖“除零”失败场景）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

NUM_CELLS = 128
CELL_RE = re.compile(r"^[A-Ha-h]([1-9]|1[0-6])$")
CONST_RE = re.compile(r"^[+-]?[0-9]+$")


def cell_index(cid: str) -> int:
    """A1..H16 → 0..127（行优先：A1=0, B1=1, ..., H1=7, A2=8, ...）；非法返回 -1。"""
    if not isinstance(cid, str) or not CELL_RE.match(cid):
        return -1
    col = ord(cid[0].upper()) - ord("A")
    row = int(cid[1:])
    return (row - 1) * 8 + col


def index_cell(idx: int) -> str:
    """0..127 → A1..H16。"""
    return chr(ord("A") + idx % 8) + str(idx // 8 + 1)


class FormulaError(Exception):
    """公式解析错误。"""


# ---------------- AST ----------------

@dataclass(frozen=True)
class Num:
    value: int


@dataclass(frozen=True)
class Ref:
    cell: str


@dataclass(frozen=True)
class Bin:
    op: str  # '+', '-', '*', '/'
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Neg:
    expr: "Expr"


@dataclass(frozen=True)
class If:
    cond: "Expr"
    yes: "Expr"
    no: "Expr"


Expr = object  # Num | Ref | Bin | Neg | If（仅作类型标注用途）


# ---------------- 递归下降解析 ----------------

class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def _ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def _peek(self) -> str:
        self._ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def _expect(self, ch: str) -> None:
        if self._peek() != ch:
            raise FormulaError(f"位置 {self.i}：期望 '{ch}'")
        self.i += 1

    def parse(self) -> Expr:
        node = self._expr()
        if self._peek():
            raise FormulaError(f"位置 {self.i}：存在无法解析的多余内容")
        return node

    def _expr(self) -> Expr:
        node = self._term()
        while True:
            c = self._peek()
            if c and c in "+-":
                self.i += 1
                node = Bin(c, node, self._term())
            else:
                return node

    def _term(self) -> Expr:
        node = self._factor()
        while True:
            c = self._peek()
            if c and c in "*/":
                self.i += 1
                node = Bin(c, node, self._factor())
            else:
                return node

    def _factor(self) -> Expr:
        c = self._peek()
        if not c:
            raise FormulaError("表达式意外结束")
        if c == "(":
            self.i += 1
            node = self._expr()
            self._expect(")")
            return node
        if c in "+-":
            self.i += 1
            d = self._peek()
            if d and d.isdigit():
                v = self._number()
                if c == "-":
                    if v == INT64_MAX + 1:
                        return Num(INT64_MIN)  # -9223372036854775808 字面量
                    return Num(-v)
                return Num(v)
            node = self._factor()
            return Neg(node) if c == "-" else node
        if c.isdigit():
            return Num(self._number())
        if c.isalpha():
            name = self._ident()
            if name.lower() == "if":
                self._expect("(")
                cond = self._expr()
                self._expect(",")
                yes = self._expr()
                self._expect(",")
                no = self._expr()
                self._expect(")")
                return If(cond, yes, no)
            if cell_index(name) < 0:
                raise FormulaError(f"无效的单元格引用：{name}")
            return Ref(name.upper())
        raise FormulaError(f"位置 {self.i}：无法识别的字符 '{c}'")

    def _number(self) -> int:
        self._ws()
        j = self.i
        while j < len(self.s) and self.s[j].isdigit():
            j += 1
        if j == self.i:
            raise FormulaError(f"位置 {self.i}：期望数字")
        text = self.s[self.i:j]
        self.i = j
        v = int(text)
        if v > INT64_MAX:
            raise FormulaError(f"整数超出 64 位范围：{text}")
        return v

    def _ident(self) -> str:
        self._ws()
        j = self.i
        while j < len(self.s) and self.s[j].isalnum():
            j += 1
        if j == self.i:
            raise FormulaError(f"位置 {self.i}：期望标识符")
        name = self.s[self.i:j]
        self.i = j
        return name


def parse(text: str) -> Expr:
    """把公式文本解析为 AST；失败抛出 FormulaError。"""
    return _Parser(text).parse()
