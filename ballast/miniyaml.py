"""A YAML subset parser used when PyYAML is not installed.

Covers what Ballast configuration files need: nested block mappings, block sequences, flow
sequences and mappings on one line, quoted and plain scalars, comments, and literal (|) and
folded (>) block scalars. Scalars resolve like PyYAML's safe_load (YAML 1.1): true/false/yes/no/on/off
are booleans, null and ~ are None, numbers are int or float. Anchors, aliases, tags and multiple
documents are refused with a message that names PyYAML.
"""

from __future__ import annotations

import re
from typing import Any

BOOLS = {"true": True, "yes": True, "on": True, "false": False, "no": False, "off": False}
INT = re.compile(r"[-+]?(0|[1-9][0-9_]*)$")
HEX = re.compile(r"0x[0-9a-fA-F_]+$")
OCT = re.compile(r"[-+]?0[0-7_]+$")
SEXAGESIMAL = re.compile(r"[-+]?[1-9][0-9_]*(:[0-5]?[0-9])+$")
FLOAT = re.compile(r"[-+]?[0-9][0-9_]*\.[0-9_]*([eE][-+][0-9]+)?$|[-+]?\.[0-9][0-9_]*([eE][-+][0-9]+)?$")
UNSUPPORTED = "uses a YAML feature this built-in reader does not support ({}); install PyYAML (pip install pyyaml) or simplify the file"


class YAMLError(ValueError):
    pass


def scalar(text: str) -> Any:
    text = text.strip()
    if text == "" or text in ("~", "null", "Null", "NULL"):
        return None
    if text[0] in "\"'":
        return _quoted(text)
    lower = text.lower()
    if lower in BOOLS and text in (lower, lower.capitalize(), lower.upper()):
        return BOOLS[lower]
    if INT.match(text):
        return int(text.replace("_", ""))
    if HEX.match(text):
        return int(text.replace("_", ""), 16)
    if OCT.match(text):
        return int(text.replace("_", ""), 8)
    if SEXAGESIMAL.match(text):
        sign, digits = (-1, text[1:]) if text[0] == "-" else (1, text.lstrip("+"))
        total = 0
        for part in digits.replace("_", "").split(":"):
            total = total * 60 + int(part)
        return sign * total
    if FLOAT.match(text) and any(c.isdigit() for c in text):
        return float(text.replace("_", ""))
    if lower in (".inf", "+.inf"):
        return float("inf")
    if lower == "-.inf":
        return float("-inf")
    if lower == ".nan":
        return float("nan")
    if text[0] in "&*!|>%@`":
        raise YAMLError(UNSUPPORTED.format(f"'{text[0]}'"))
    return text


def _quoted(text: str) -> str:
    quote = text[0]
    if len(text) < 2 or text[-1] != quote:
        raise YAMLError(f"unterminated string: {text}")
    body = text[1:-1]
    if quote == "'":
        return body.replace("''", "'")
    out, i = [], 0
    escapes = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/", " ": " "}
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if nxt in "xuU":
                width = {"x": 2, "u": 4, "U": 8}[nxt]
                out.append(chr(int(body[i + 2 : i + 2 + width], 16)))
                i += 2 + width
                continue
            raise YAMLError(f"unknown escape \\{nxt} in {text}")
        out.append(c)
        i += 1
    return "".join(out)


def _strip_comment(line: str) -> str:
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'" and (i == 0 or line[i - 1] in " \t[{,:"):
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _split_flow(body: str) -> list[str]:
    parts, depth, quote, start = [], 0, None, 0
    for i, c in enumerate(body):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    return [p.strip() for p in parts if p.strip()]


def _split_key(text: str) -> tuple[str, str] | None:
    quote = None
    for i, c in enumerate(text):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'" and i == 0:
            quote = c
        elif c == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
            key = text[:i].strip()
            return (_quoted(key) if key[:1] in "\"'" else key), text[i + 1 :].strip()
    return None


def value(text: str) -> Any:
    text = text.strip()
    if text.startswith("["):
        if not text.endswith("]"):
            raise YAMLError(f"unterminated list: {text}")
        return [value(p) for p in _split_flow(text[1:-1])]
    if text.startswith("{"):
        if not text.endswith("}"):
            raise YAMLError(f"unterminated mapping: {text}")
        out = {}
        for part in _split_flow(text[1:-1]):
            kv = _split_key(part) or (part, "")
            out[kv[0]] = value(kv[1])
        return out
    return scalar(text)


class _Parser:
    def __init__(self, text: str):
        self.lines: list[tuple[int, str, str]] = []
        for number, raw in enumerate(text.splitlines(), 1):
            if raw.strip() in ("---", "...") and not self.lines:
                continue
            if raw.strip() in ("---", "..."):
                raise YAMLError(UNSUPPORTED.format("multiple documents"))
            if "\t" in raw[: len(raw) - len(raw.lstrip())]:
                raise YAMLError(f"line {number}: tabs are not allowed for indentation")
            self.lines.append((number, raw, _strip_comment(raw)))
        self.pos = 0

    def _next(self) -> tuple[int, int, str] | None:
        while self.pos < len(self.lines):
            number, _, line = self.lines[self.pos]
            if line.strip():
                return number, len(line) - len(line.lstrip()), line.strip()
            self.pos += 1
        return None

    def parse(self) -> Any:
        head = self._next()
        if head is None:
            return None
        result = self.block(head[1])
        rest = self._next()
        if rest is not None:
            raise YAMLError(f"line {rest[0]}: unexpected indentation")
        return result

    def block(self, indent: int) -> Any:
        head = self._next()
        assert head is not None
        if head[2] == "-" or head[2].startswith("- "):
            return self.sequence(indent)
        if _split_key(head[2]) is None:
            self.pos += 1
            return value(head[2])
        return self.mapping(indent)

    def _block_scalar(self, style: str, parent: int) -> str:
        collected: list[str] = []
        block_indent = None
        while self.pos < len(self.lines):
            _, raw, _ = self.lines[self.pos]
            if raw.strip():
                width = len(raw) - len(raw.lstrip())
                if width <= parent:
                    break
                block_indent = width if block_indent is None else block_indent
                collected.append(raw[block_indent:])
            else:
                collected.append("")
            self.pos += 1
        while collected and not collected[-1]:
            collected.pop()
        keep = style.endswith("+")
        if style.startswith(">"):
            text = " ".join(line if line else "\n" for line in collected).replace(" \n ", "\n")
        else:
            text = "\n".join(collected)
        return text + ("\n" if collected or keep else "")

    def _child(self, rest: str, indent: int, number: int) -> Any:
        if rest in ("|", "|-", "|+", ">", ">-", ">+"):
            text = self._block_scalar(rest, indent)
            return text.rstrip("\n") if rest.endswith("-") else text
        if rest:
            return value(rest)
        nxt = self._next()
        if nxt is None or nxt[1] < indent or (nxt[1] == indent and not nxt[2].startswith("- ") and nxt[2] != "-"):
            return None
        if nxt[1] == indent:
            return self.sequence(indent)
        return self.block(nxt[1])

    def mapping(self, indent: int) -> dict:
        out: dict = {}
        while (head := self._next()) is not None:
            number, width, line = head
            if width < indent:
                break
            if width > indent:
                raise YAMLError(f"line {number}: unexpected indentation")
            kv = _split_key(line)
            if kv is None:
                raise YAMLError(f"line {number}: expected 'key: value'")
            key, rest = kv
            if key in out:
                raise YAMLError(f"line {number}: duplicate key '{key}'")
            self.pos += 1
            out[key] = self._child(rest, indent, number)
        return out

    def sequence(self, indent: int) -> list:
        out: list = []
        while (head := self._next()) is not None:
            number, width, line = head
            if width < indent or not (line == "-" or line.startswith("- ")):
                break
            if width > indent:
                raise YAMLError(f"line {number}: unexpected indentation")
            rest = line[1:].strip()
            if rest and _split_key(rest) is not None and not rest.startswith(("[", "{", '"', "'")):
                inner = width + 2 + (len(line) - 2 - len(line[2:].lstrip()))
                number_, raw, stripped = self.lines[self.pos]
                self.lines[self.pos] = (number_, raw, " " * inner + rest)
                out.append(self.mapping(inner))
            else:
                self.pos += 1
                out.append(self._child(rest, width + 1, number) if rest else self._child("", width + 1, number))
        return out


def safe_load(text: str) -> Any:
    return _Parser(text).parse()
