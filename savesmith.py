#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wake-up-savesmith — 存档快照比对与安全改写

它做两件事：
  1. diff —— 两份存档快照对齐，列出变化的字段路径 + 原值 / 现值。
  2. edit —— 只替换目标值的那一小段文本，带备份、原子写、回读校验。

它不认识任何游戏，不判断字段语义，不复活 NPC。把「哪个字段该改成什么」
留给使用者；工具只负责「找得准」和「不改坏」。

零第三方依赖，Python 3.9+，clone 即可运行。

支持的存档形态：一个或多个 JSON 值拼接成的文本块，块之间用
`~SettingsSplit~`、`;` 或换行分隔（Unity 的常见写法），也支持单个
JSON 对象和 JSON Lines。不是这套结构的文件一律拒绝改写。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

__version__ = "0.1.0"

SETTINGS_SPLIT = "~SettingsSplit~"
WS = " \t\r\n"
_MISS = object()

# 只用来「提醒」，不做任何自动处理：这类字段往往意味着存档带完整性校验，
# 直接改值可能让游戏拒绝读档。命中时 edit 要求 --force。
SUSPICIOUS_WORDS = frozenset({
    "checksum", "checksums", "hash", "hashed", "signature", "sig",
    "crc", "crc32", "md5", "sha1", "sha256", "sha512", "hmac",
    "digest", "integrity", "verify", "verification",
})
# camelCase / PascalCase 拆词。不能拿 "hash" 这种短词做子串匹配：
# "hasHooveredFirstItem" 的前四个字母恰好是 h-a-s-h，会误报。
WORD_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
PATH_TOKEN_RE = re.compile(r"[^.\[\]]+|\[\d+\]")


def _path_words(component: str):
    spaced = WORD_SPLIT_RE.sub(" ", component)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def is_suspicious_path(path: tuple) -> bool:
    return any(
        w in SUSPICIOUS_WORDS
        for comp in path
        if isinstance(comp, str)
        for w in _path_words(comp)
    )


class SaveFormatError(Exception):
    """存档结构不符合预期，或校验未通过。"""


# --------------------------------------------------------------------------
# 读取 / 编码
# --------------------------------------------------------------------------

def read_text(path: Path):
    """返回 (text, encoding_label, bom_bytes, encoding_name, raw_bytes)。

    按字节读、按字节写，保证换行符和 BOM 原样保留。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        enc, bom, label = "utf-8", b"\xef\xbb\xbf", "utf-8 (BOM)"
    elif raw.startswith(b"\xff\xfe"):
        enc, bom, label = "utf-16-le", b"\xff\xfe", "utf-16-le (BOM)"
    elif raw.startswith(b"\xfe\xff"):
        enc, bom, label = "utf-16-be", b"\xfe\xff", "utf-16-be (BOM)"
    else:
        enc, bom, label = "utf-8", b"", "utf-8 (no BOM)"
    try:
        text = raw[len(bom):].decode(enc)
    except UnicodeDecodeError as exc:
        raise SaveFormatError(f"cannot decode {path} as {enc}: {exc}") from exc
    return text, label, bom, enc, raw


def encode_text(text: str, bom: bytes, enc: str) -> bytes:
    return bom + text.encode(enc)


# --------------------------------------------------------------------------
# JSON 扫描器：解析一个值，同时记下每个路径的「值文本」起止偏移
# --------------------------------------------------------------------------

class JsonScanner:
    """扫描 text 中从绝对偏移 i 开始的单个 JSON 值。

    spans: dict[tuple[str | int, ...], tuple[int, int]]
           key 是相对该值根节点的路径，value 是「值 token」在 text 中的绝对区间。
           有了它就能做外科手术式替换，而不是 json.dumps 整块重写。
    """

    def __init__(self, text: str):
        self.t = text
        self.n = len(text)
        self.spans: dict[tuple, tuple[int, int]] = {}

    def _ws(self, i: int) -> int:
        t, n = self.t, self.n
        while i < n and t[i] in WS:
            i += 1
        return i

    def _string(self, i: int):
        t, n = self.t, self.n
        if i >= n or t[i] != '"':
            raise SaveFormatError(f"expected string at offset {i}")
        start = i
        i += 1
        while i < n:
            c = t[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                i += 1
                try:
                    return json.loads(t[start:i]), i
                except json.JSONDecodeError as exc:
                    raise SaveFormatError(f"bad string at {start}: {exc}") from exc
            i += 1
        raise SaveFormatError(f"unterminated string at {start}")

    def value(self, i: int, path: tuple):
        t, n = self.t, self.n
        i = self._ws(i)
        if i >= n:
            raise SaveFormatError(f"unexpected end of input at {i}")
        start = i
        c = t[i]

        if c == "{":
            i += 1
            obj = {}
            i = self._ws(i)
            if i < n and t[i] == "}":
                i += 1
                self.spans[path] = (start, i)
                return obj, i
            while True:
                i = self._ws(i)
                key, i = self._string(i)
                i = self._ws(i)
                if i >= n or t[i] != ":":
                    raise SaveFormatError(f"expected ':' after key at {i}")
                i += 1
                val, i = self.value(i, path + (key,))
                obj[key] = val
                i = self._ws(i)
                if i < n and t[i] == ",":
                    i += 1
                    continue
                if i < n and t[i] == "}":
                    i += 1
                    break
                raise SaveFormatError(f"expected ',' or '}}' at {i}")
            self.spans[path] = (start, i)
            return obj, i

        if c == "[":
            i += 1
            arr = []
            i = self._ws(i)
            if i < n and t[i] == "]":
                i += 1
                self.spans[path] = (start, i)
                return arr, i
            idx = 0
            while True:
                val, i = self.value(i, path + (idx,))
                arr.append(val)
                idx += 1
                i = self._ws(i)
                if i < n and t[i] == ",":
                    i += 1
                    continue
                if i < n and t[i] == "]":
                    i += 1
                    break
                raise SaveFormatError(f"expected ',' or ']' at {i}")
            self.spans[path] = (start, i)
            return arr, i

        if c == '"':
            s, i = self._string(i)
            self.spans[path] = (start, i)
            return s, i

        # number / true / false / null
        j = i
        while j < n and t[j] not in WS + ",}]":
            j += 1
        token = t[i:j]
        if j == i:
            raise SaveFormatError(f"empty scalar at {i}")
        if token == "true":
            val = True
        elif token == "false":
            val = False
        elif token == "null":
            val = None
        else:
            try:
                val = json.loads(token)
            except json.JSONDecodeError as exc:
                raise SaveFormatError(f"bad scalar {token!r} at {i}: {exc}") from exc
        self.spans[path] = (start, j)
        return val, j


@dataclass
class Block:
    index: int
    start: int
    end: int
    text: str
    value: object
    scanner: JsonScanner
    gap: str  # 该块之后、下一块之前的原始分隔文本


def scan_blocks(text: str) -> list[Block]:
    """把整份文本切成若干顶层 JSON 值，保留分隔文本以便回写校验。"""
    blocks: list[Block] = []
    n = len(text)
    i = 0
    while True:
        while i < n and text[i] in WS:
            i += 1
        if i >= n:
            break
        scanner = JsonScanner(text)
        try:
            value, end = scanner.value(i, ())
        except SaveFormatError as exc:
            raise SaveFormatError(f"block #{len(blocks)} at offset {i}: {exc}") from exc
        j = end
        gap_start = end
        while j < n:
            if text[j] in WS:
                j += 1
                continue
            if text[j] == ";":
                j += 1
                continue
            if text.startswith(SETTINGS_SPLIT, j):
                j += len(SETTINGS_SPLIT)
                continue
            break
        blocks.append(
            Block(len(blocks), i, end, text[i:end], value, scanner, text[gap_start:j])
        )
        i = j
    return blocks


# --------------------------------------------------------------------------
# 路径 / 扁平化
# --------------------------------------------------------------------------

def render_path(block_index: int, path: tuple) -> str:
    s = f"b{block_index}:"
    for comp in path:
        if isinstance(comp, int):
            s += f"[{comp}]"
        else:
            if not s.endswith(":"):
                s += "."
            s += str(comp)
    return s


def is_scalar_list(v) -> bool:
    return isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v)


def flatten(value, prefix: tuple = (), out=None) -> dict:
    """把嵌套结构摊平成 {路径: 值}。

    纯标量数组整体当作一个叶子（对比时按「集合增减」报，而不是按下标，
    否则数组中间插入一个元素会把后面所有下标都报成变化，全是噪音）。
    """
    if out is None:
        out = {}
    if isinstance(value, dict):
        if not value and prefix:
            out[prefix] = {}
        for k, v in value.items():
            flatten(v, prefix + (str(k),), out)
    elif isinstance(value, list):
        if is_scalar_list(value):
            out[prefix] = list(value)
        else:
            for idx, v in enumerate(value):
                flatten(v, prefix + (idx,), out)
    else:
        out[prefix] = value
    return out


def flat_document(blocks: list[Block]) -> dict:
    out = {}
    for b in blocks:
        for path, val in flatten(b.value).items():
            out[render_path(b.index, path)] = val
    return out


def _key(x) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True)


def list_delta(a: list, b: list):
    """保序地算出 b 相对 a 新增 / 删除的元素（多重集合）。"""
    removed, added = [], []
    pool = Counter(_key(x) for x in b)
    for x in a:
        k = _key(x)
        if pool[k] > 0:
            pool[k] -= 1
        else:
            removed.append(x)
    pool = Counter(_key(x) for x in a)
    for x in b:
        k = _key(x)
        if pool[k] > 0:
            pool[k] -= 1
        else:
            added.append(x)
    return removed, added


def fmt(v, limit: int = 72) -> str:
    s = json.dumps(v, ensure_ascii=False)
    if len(s) > limit:
        s = s[: limit - 3] + "..."
    return s


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

def walk_keys(value, path: tuple = (), out=None) -> list:
    if out is None:
        out = []
    if isinstance(value, dict):
        for k, v in value.items():
            p = path + (str(k),)
            out.append(p)
            walk_keys(v, p, out)
    elif isinstance(value, list):
        for idx, v in enumerate(value):
            walk_keys(v, path + (idx,), out)
    return out


def find_suspicious(blocks: list[Block]) -> list[str]:
    hits = set()
    for b in blocks:
        for p in walk_keys(b.value):
            if is_suspicious_path(p):
                hits.add(render_path(b.index, p))
    return sorted(hits)


def probe_info(path: Path, blocks: list[Block], label: str, raw: bytes) -> dict:
    gaps = [b.gap for b in blocks]
    semis = sum(g.count(";") for g in gaps)
    splits = sum(g.count(SETTINGS_SPLIT) for g in gaps)
    if not blocks:
        shape = "unknown (no JSON value found)"
    elif len(blocks) == 1:
        shape = "single-json"
    elif semis or splits:
        shape = f"multi-json-blocks ({len(blocks)} blocks)"
    else:
        shape = f"json-lines ({len(blocks)} blocks)"

    delimiters = []
    if splits:
        delimiters.append(f"{SETTINGS_SPLIT} x{splits}")
    if semis:
        delimiters.append(f"; x{semis}")
    if not delimiters and len(blocks) > 1:
        delimiters.append("newline")

    suspicious = find_suspicious(blocks)
    if not blocks:
        verdict = "UNREADABLE"
        reason = "no parseable JSON value — edit refused"
    elif suspicious:
        verdict = "CAUTION"
        reason = "possible integrity field — edit needs --force"
    else:
        verdict = "EDITABLE"
        reason = "diff / edit supported"

    return {
        "tool": "wake-up-savesmith",
        "version": __version__,
        "path": str(path),
        "size": len(raw),
        "encoding": label,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "shape": shape,
        "blocks": len(blocks),
        "delimiters": delimiters,
        "parse": "all blocks parse as JSON" if blocks else "failed",
        "suspicious_keys": suspicious,
        "verdict": verdict,
        "reason": reason,
    }


def print_probe(info: dict) -> None:
    print(f"wake-up-savesmith {info['version']} — probe")
    print(f"  file:       {info['path']}")
    print(f"  size:       {info['size']} bytes")
    print(f"  encoding:   {info['encoding']}")
    print(f"  sha256:     {info['sha256']}")
    print(f"  format:     {info['shape']}")
    print(f"  delimiters: {', '.join(info['delimiters']) or '(none)'}")
    print(f"  parse:      {info['parse']}")
    if info["suspicious_keys"]:
        print("  suspicious keys (possible checksum / signature):")
        for k in info["suspicious_keys"][:12]:
            print(f"    - {k}")
        if len(info["suspicious_keys"]) > 12:
            print(f"    ... and {len(info['suspicious_keys']) - 12} more")
    else:
        print("  suspicious keys: none")
    print(f"  verdict:    {info['verdict']}  ({info['reason']})")


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

def diff_documents(blocks_a, blocks_b, ignore_res):
    fa, fb = flat_document(blocks_a), flat_document(blocks_b)
    if ignore_res:
        fa = {k: v for k, v in fa.items() if not any(r.search(k) for r in ignore_res)}
        fb = {k: v for k, v in fb.items() if not any(r.search(k) for r in ignore_res)}
    changed, added, removed, lists = [], [], [], []
    for k in sorted(set(fa) | set(fb)):
        if k in fa and k not in fb:
            removed.append({"path": k, "from": fa[k]})
        elif k not in fa and k in fb:
            added.append({"path": k, "to": fb[k]})
        else:
            a, b = fa[k], fb[k]
            if a == b:
                continue
            if isinstance(a, list) and isinstance(b, list):
                rem, add = list_delta(a, b)
                lists.append({
                    "path": k, "from_len": len(a), "to_len": len(b),
                    "added": add, "removed": rem,
                })
            else:
                changed.append({"path": k, "from": a, "to": b})
    return changed, added, removed, lists


def print_diff(args, info_a, info_b, changed, added, removed, lists) -> None:
    print(f"wake-up-savesmith {__version__} — diff")
    print(f"  A: {args.a}  ({info_a['blocks']} blocks, {info_a['size']} bytes)")
    print(f"  B: {args.b}  ({info_b['blocks']} blocks, {info_b['size']} bytes)")
    if info_a["blocks"] != info_b["blocks"]:
        print(f"  ! block count differs ({info_a['blocks']} vs {info_b['blocks']}); "
              f"aligned by index, extra blocks reported as added/removed")
        print("    alignment: by index (partial)")
    else:
        print(f"    alignment: by index ({info_a['blocks']} = {info_b['blocks']})  OK")
    if args.ignore:
        print(f"    ignored:   {', '.join(args.ignore)}")
    print()

    if changed:
        print(f"  CHANGED ({len(changed)})")
        for r in changed:
            print(f"    {r['path']:<44} {fmt(r['from'])} -> {fmt(r['to'])}")
        print()
    if lists:
        print(f"  LIST CHANGES ({len(lists)})")
        for r in lists:
            print(f"    {r['path']:<44} {r['from_len']} -> {r['to_len']} items")
            if r["added"]:
                sample = ", ".join(fmt(x, 30) for x in r["added"][: args.max_list_items])
                print(f"      +{len(r['added'])}: {sample}")
            if r["removed"]:
                sample = ", ".join(fmt(x, 30) for x in r["removed"][: args.max_list_items])
                print(f"      -{len(r['removed'])}: {sample}")
        print()
    if added:
        print(f"  ADDED ({len(added)})")
        for r in added:
            print(f"    {r['path']:<44} (none) -> {fmt(r['to'])}")
        print()
    if removed:
        print(f"  REMOVED ({len(removed)})")
        for r in removed:
            print(f"    {r['path']:<44} {fmt(r['from'])} -> (none)")
        print()
    if not (changed or added or removed or lists):
        print("  no differences.")
    else:
        print(f"  total: {len(changed)} changed, {len(lists)} list changes, "
              f"{len(added)} added, {len(removed)} removed")


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------

def parse_scalar(s: str):
    """值按 JSON 解析；解析不了就当字符串。"""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s


def parse_inner(inner: str) -> tuple:
    toks = PATH_TOKEN_RE.findall(inner)
    path = []
    for t in toks:
        if t.startswith("[") and t.endswith("]"):
            path.append(int(t[1:-1]))
        else:
            path.append(t)
    return tuple(path)


def parse_set(spec: str):
    if "=" not in spec:
        raise SaveFormatError(f"--set needs PATH=VALUE, got {spec!r}")
    path_str, val_str = spec.split("=", 1)
    m = re.match(r"^b(\d+):(.*)$", path_str)
    if m:
        idx, inner = int(m.group(1)), m.group(2)
    else:
        idx, inner = None, path_str
    inner_path = parse_inner(inner)
    if not inner_path:
        raise SaveFormatError(f"empty path in --set {spec!r}")
    return idx, inner_path, parse_scalar(val_str), path_str


def resolve_block(blocks, inner_path, explicit_idx=None) -> Block:
    if explicit_idx is not None:
        if explicit_idx < 0 or explicit_idx >= len(blocks):
            raise SaveFormatError(f"block index {explicit_idx} out of range (0..{len(blocks)-1})")
        return blocks[explicit_idx]
    if not isinstance(inner_path[0], str):
        raise SaveFormatError("auto-locate needs a top-level key name; use bN:path")
    key = inner_path[0]
    hits = [b for b in blocks if isinstance(b.value, dict) and key in b.value]
    if len(hits) != 1:
        raise SaveFormatError(
            f"key {key!r} appears in {len(hits)} blocks; "
            f"use bN:{'.'.join(map(str, inner_path))} to disambiguate"
        )
    return hits[0]


def flatten_key(block: Block, inner_path: tuple) -> str:
    """把 --set 路径换算成 flat_document 里的键，用于校验改动范围。"""
    cur = block.value
    p: tuple = ()
    for comp in inner_path:
        if isinstance(cur, dict) and isinstance(comp, str):
            if comp not in cur:
                raise SaveFormatError(f"path component {comp!r} not found")
            cur = cur[comp]
            p += (comp,)
        elif isinstance(cur, list) and isinstance(comp, int):
            if is_scalar_list(cur):
                return render_path(block.index, p)
            if comp < 0 or comp >= len(cur):
                raise SaveFormatError(f"array index {comp} out of range")
            cur = cur[comp]
            p += (comp,)
        elif isinstance(cur, list) and is_scalar_list(cur):
            return render_path(block.index, p)
        else:
            raise SaveFormatError(f"cannot descend into {type(cur).__name__} at {render_path(block.index, p)}")
    return render_path(block.index, p)


def apply_sets(blocks, text, sets):
    """外科手术式替换：只改目标值的 token，其余字节原封不动。"""
    edits = []
    for idx, inner_path, value, path_str in sets:
        block = resolve_block(blocks, inner_path, idx)
        if inner_path not in block.scanner.spans:
            raise SaveFormatError(
                f"path not found: {render_path(block.index, inner_path)} "
                f"(edit only sets existing paths; it does not add keys)"
            )
        s, e = block.scanner.spans[inner_path]
        token = json.dumps(value, ensure_ascii=False)
        edits.append((s, e, token, render_path(block.index, inner_path), path_str))

    edits.sort(key=lambda x: x[0])
    for prev, nxt in zip(edits, edits[1:]):
        if prev[1] > nxt[0]:
            raise SaveFormatError(
                f"overlapping --set paths: {prev[3]} and {nxt[3]}"
            )

    parts, last = [], 0
    for s, e, token, _, _ in edits:
        parts.append(text[last:s])
        parts.append(token)
        last = e
    parts.append(text[last:])
    return "".join(parts), edits


def verify(text_before, blocks_before, text_after, sets):
    """写盘之前在内存里做的全部校验；任何一条不过就绝不落盘。"""
    blocks_after = scan_blocks(text_after)
    if len(blocks_after) != len(blocks_before):
        raise SaveFormatError(
            f"block count changed: {len(blocks_before)} -> {len(blocks_after)}"
        )
    for a, b in zip(blocks_before, blocks_after):
        if a.gap != b.gap:
            raise SaveFormatError(f"delimiter changed at block {a.index}")

    fa, fb = flat_document(blocks_before), flat_document(blocks_after)
    changed = {k for k in set(fa) | set(fb) if fa.get(k, _MISS) != fb.get(k, _MISS)}
    allowed = set()
    for idx, inner_path, _, _ in sets:
        block = resolve_block(blocks_before, inner_path, idx)
        allowed.add(flatten_key(block, inner_path))
    extra = changed - allowed
    if extra:
        raise SaveFormatError("unintended changes detected: " + ", ".join(sorted(extra)))

    for idx, inner_path, value, _ in sets:
        block = resolve_block(blocks_after, inner_path, idx)
        s, e = block.scanner.spans[inner_path]
        got = json.loads(text_after[s:e])
        if got != value:
            raise SaveFormatError(
                f"readback mismatch at {render_path(block.index, inner_path)}: "
                f"{got!r} != {value!r}"
            )
    return blocks_after


def make_backup(path: Path, raw: bytes, mode: int, explicit=None) -> Path:
    if explicit:
        target = Path(explicit)
        if target.exists():
            raise SaveFormatError(f"backup target already exists: {target}")
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = path.with_name(path.name + ".bak-" + stamp)
        n = 1
        while target.exists():
            target = path.with_name(path.name + f".bak-{stamp}-{n}")
            n += 1
    target.write_bytes(raw)
    os.chmod(target, mode)
    return target


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="wake-up-savesmith",
        description="存档快照比对与安全改写（不认识任何游戏，只负责找得准、不改坏）",
    )
    p.add_argument("--version", action="version",
                   version=f"wake-up-savesmith {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("probe", help="只读探查存档结构与风险")
    q.add_argument("file")
    q.add_argument("--json", action="store_true", help="输出 JSON")

    d = sub.add_parser("diff", help="比较两份存档快照")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--ignore", action="append", default=[], metavar="REGEX",
                   help="按路径正则过滤噪音（可重复），如 --ignore playTime")
    d.add_argument("--max-list-items", type=int, default=3, metavar="N",
                   help="数组增减的样例条数（默认 3）")
    d.add_argument("--json", action="store_true", help="输出 JSON")

    e = sub.add_parser("edit", help="安全改写：只改目标值，带备份/原子写/回读校验")
    e.add_argument("file")
    e.add_argument("--set", dest="sets", action="append", required=True,
                   metavar="PATH=VALUE", help="要改的字段（可重复）。值为 JSON；"
                   "路径可写 b3:key.sub[0] 或直接写顶层 key（自动定位唯一块）")
    e.add_argument("--backup", metavar="PATH", help="指定备份路径（默认自动生成）")
    e.add_argument("--dry-run", action="store_true", help="只校验并预览，不写盘")
    e.add_argument("--force", action="store_true",
                   help="即使 probe 判为 CAUTION 也继续（自行承担校验和失效风险）")
    return p


def load(path_str):
    p = Path(path_str)
    if not p.is_file():
        raise SaveFormatError(f"file not found: {p}")
    text, label, bom, enc, raw = read_text(p)
    try:
        blocks = scan_blocks(text)
    except SaveFormatError:
        blocks = []
        parse_error = True
    else:
        parse_error = False
    return p, text, label, bom, enc, raw, blocks, parse_error


def cmd_probe(args):
    p, text, label, bom, enc, raw, blocks, parse_error = load(args.file)
    info = probe_info(p, blocks, label, raw)
    if parse_error:
        info["verdict"] = "UNREADABLE"
        info["reason"] = "parse error — edit refused"
        info["parse"] = "failed"
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print_probe(info)
    return 0 if info["verdict"] != "UNREADABLE" else 2


def cmd_diff(args):
    ignore_res = []
    for pat in args.ignore:
        try:
            ignore_res.append(re.compile(pat))
        except re.error as exc:
            raise SaveFormatError(f"bad --ignore regex {pat!r}: {exc}") from exc
    pa, ta, la, ba, ea, ra, blocks_a, err_a = load(args.a)
    pb, tb, lb, bb, eb, rb, blocks_b, err_b = load(args.b)
    if err_a or err_b:
        raise SaveFormatError("one of the files is not parseable as JSON blocks")
    info_a = probe_info(pa, blocks_a, la, ra)
    info_b = probe_info(pb, blocks_b, lb, rb)
    changed, added, removed, lists = diff_documents(blocks_a, blocks_b, ignore_res)
    if args.json:
        print(json.dumps({
            "tool": "wake-up-savesmith", "version": __version__,
            "a": {"path": str(pa), "blocks": info_a["blocks"], "size": info_a["size"]},
            "b": {"path": str(pb), "blocks": info_b["blocks"], "size": info_b["size"]},
            "alignment": "by-index",
            "ignored": args.ignore,
            "changed": changed, "added": added, "removed": removed,
            "list_changes": lists,
        }, ensure_ascii=False, indent=2))
    else:
        print_diff(args, info_a, info_b, changed, added, removed, lists)
    return 0


def cmd_edit(args):
    p, text, label, bom, enc, raw, blocks, parse_error = load(args.file)
    if parse_error or not blocks:
        raise SaveFormatError("refusing to edit: file is not parseable as JSON blocks; run `probe` first")
    info = probe_info(p, blocks, label, raw)
    if info["verdict"] == "CAUTION" and not args.force:
        raise SaveFormatError(
            "probe says CAUTION (possible checksum/signature field). "
            "Editing may invalidate the save. Re-run with --force only if you "
            "know how to recompute it."
        )

    sets = [parse_set(s) for s in args.sets]
    new_text, edits = apply_sets(blocks, text, sets)
    blocks_after = verify(text, blocks, new_text, sets)

    print(f"wake-up-savesmith {__version__} — edit")
    print(f"  file:     {p}")
    print(f"  format:   {info['shape']}  ({info['encoding']})")
    print(f"  sha256:   {info['sha256']}  (before)")
    print(f"  checks:   {len(blocks)} -> {len(blocks_after)} blocks OK; "
          f"delimiters OK; change scope OK; readback OK")
    print("  edits:")
    for s, e, token, rendered, path_str in edits:
        old = text[s:e]
        print(f"    {rendered}")
        print(f"      {fmt(old, 60)}  ->  {fmt(token, 60)}")
    if args.dry_run:
        print("  dry-run: no backup, no write. file untouched.")
        return 0

    mode = p.stat().st_mode
    backup = make_backup(p, raw, mode, args.backup)
    data = encode_text(new_text, bom, enc)
    atomic_write(p, data, mode)
    # 落盘后再读一次，确认磁盘上的字节就是我们验证过的那份
    if p.read_bytes() != data:
        raise SaveFormatError(f"post-write byte mismatch; restore from {backup}")
    print(f"  backup:   {backup}")
    print(f"  written:  {p}  ({len(data)} bytes)")
    print("  next:     进游戏验证（Steam 若弹云存档冲突，务必选本地存档）")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "probe":
            return cmd_probe(args)
        if args.cmd == "diff":
            return cmd_diff(args)
        if args.cmd == "edit":
            return cmd_edit(args)
    except (SaveFormatError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
