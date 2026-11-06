# -*- coding: utf-8 -*-
"""wake-up-savesmith 单元测试。零第三方依赖，只用 unittest + 标准库。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "savesmith.py"
sys.path.insert(0, str(ROOT))
import savesmith as S  # noqa: E402


def run_cli(*args):
    """用当前解释器跑 CLI，返回 (returncode, stdout, stderr)。

    子进程 stdout 默认跟系统代码页（Windows 上是 cp936），而 CLI 会打印中文和
    em dash，按 utf-8 解码会炸。强制子进程用 UTF-8 输出，测试才能跨平台干净。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


class TestScannerAndBlocks(unittest.TestCase):
    def test_spans_record_absolute_offsets(self):
        text = '{"a": 1, "b": {"c": [true, null, "x"]}}'
        sc = S.JsonScanner(text)
        value, end = sc.value(0, ())
        self.assertEqual(value, {"a": 1, "b": {"c": [True, None, "x"]}})
        self.assertEqual(end, len(text))
        # 标量 token 的区间能被切回原文本
        s, e = sc.spans[("a",)]
        self.assertEqual(text[s:e], "1")
        s, e = sc.spans[("b", "c", 2)]
        self.assertEqual(text[s:e], '"x"')

    def test_suspicious_whole_word_not_substring(self):
        # 回归：旧版按子串匹配 "hash"，把 hasHooveredFirstItem 误报成校验和字段
        self.assertTrue(S.is_suspicious_path(("data", "checksum")))
        self.assertTrue(S.is_suspicious_path(("save", "Signature")))
        self.assertTrue(S.is_suspicious_path(("sha256Hash",)))
        self.assertFalse(S.is_suspicious_path(("hasHooveredFirstItem",)))
        self.assertFalse(S.is_suspicious_path(("hasHeardWunderBark",)))
        self.assertFalse(S.is_suspicious_path(("steamTownTrader",)))

    def test_scan_blocks_preserves_delimiters(self):
        text = '{"a":1};{"b":2}~SettingsSplit~\n{"c":3}'
        blocks = S.scan_blocks(text)
        self.assertEqual(len(blocks), 3)
        self.assertEqual([b.value for b in blocks], [{"a": 1}, {"b": 2}, {"c": 3}])
        self.assertIn(";", blocks[0].gap)
        self.assertIn("~SettingsSplit~", blocks[1].gap)
        # 拼回去应等于原文（Block.text 是该块自身的原始文本）
        rebuilt = "".join(b.text + b.gap for b in blocks)
        self.assertEqual(rebuilt, text)

    def test_read_text_preserves_bom_and_newlines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.txt"
            p.write_bytes(b"\xef\xbb\xbf" + b'{"a":1}\r\n')
            text, label, bom, enc, raw = S.read_text(p)
            self.assertEqual(bom, b"\xef\xbb\xbf")
            self.assertEqual(label, "utf-8 (BOM)")
            self.assertIn("\r\n", text)
            self.assertEqual(S.encode_text(text, bom, enc), raw)


class TestDiff(unittest.TestCase):
    def test_changed_added_removed_and_list_delta(self):
        a = S.scan_blocks('{"x":1,"keep":5};{"items":[1,2]}')
        b = S.scan_blocks('{"x":2,"new":9};{"items":[1,2,3]}')
        changed, added, removed, lists = S.diff_documents(a, b, [])
        self.assertIn({"path": "b0:x", "from": 1, "to": 2}, changed)
        self.assertTrue(any(r["path"] == "b0:new" for r in added))
        self.assertTrue(any(r["path"] == "b0:keep" for r in removed))
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["added"], [3])
        self.assertEqual(lists[0]["removed"], [])

    def test_ignore_filters_noise(self):
        a = S.scan_blocks('{"playTime":1,"hp":10}')
        b = S.scan_blocks('{"playTime":2,"hp":10}')
        changed, _, _, _ = S.diff_documents(a, b, [S.re.compile("playTime")])
        self.assertEqual(changed, [])

    def test_scalar_list_is_atomic_not_index_diff(self):
        a = S.scan_blocks('{"ids":[10,20,30]}')
        b = S.scan_blocks('{"ids":[10,99,20,30]}')
        changed, _, _, lists = S.diff_documents(a, b, [])
        self.assertEqual(changed, [])  # 不是把 [1]/[2]/[3] 全报成变化
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["added"], [99])


class TestEdit(unittest.TestCase):
    def test_surgical_edit_roundtrip(self):
        text = '{"a":1,"b":2};{"c":3}'
        blocks = S.scan_blocks(text)
        sets = [S.parse_set("b0:a=9")]
        new_text, edits = S.apply_sets(blocks, text, sets)
        self.assertEqual(new_text, '{"a":9,"b":2};{"c":3}')
        S.verify(text, blocks, new_text, sets)  # 不抛异常即通过

    def test_verify_rejects_unintended_changes(self):
        text = '{"a":1,"b":2}'
        blocks = S.scan_blocks(text)
        sets = [S.parse_set("b0:a=9")]
        tampered = '{"a":9,"b":99}'  # 顺手把 b 也改了
        with self.assertRaises(S.SaveFormatError) as ctx:
            S.verify(text, blocks, tampered, sets)
        self.assertIn("unintended changes", str(ctx.exception))

    def test_edit_refuses_missing_path(self):
        text = '{"a":1}'
        blocks = S.scan_blocks(text)
        with self.assertRaises(S.SaveFormatError):
            S.apply_sets(blocks, text, [S.parse_set("b0:nope=5")])

    def test_auto_locate_unique_key(self):
        text = '{"first":1};{"target":2}'
        blocks = S.scan_blocks(text)
        sets = [S.parse_set("target=7")]  # 不带块号
        new_text, _ = S.apply_sets(blocks, text, sets)
        self.assertEqual(new_text, '{"first":1};{"target":7}')

    def test_caution_requires_force(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "save.txt"
            p.write_text('{"checksum":"abc","hp":10}', encoding="utf-8")
            info = S.probe_info(p, S.scan_blocks(p.read_text(encoding="utf-8")),
                                "utf-8 (no BOM)", p.read_bytes())
            self.assertEqual(info["verdict"], "CAUTION")
            r = run_cli("edit", str(p), "--set", "b0:hp=5")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("--force", r.stderr)


class TestCli(unittest.TestCase):
    def test_version(self):
        r = run_cli("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn(S.__version__, r.stdout)

    def test_probe_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.txt"
            p.write_text('{"a":1};{"b":2}', encoding="utf-8")
            r = run_cli("probe", str(p), "--json")
            self.assertEqual(r.returncode, 0)
            data = json.loads(r.stdout)
            self.assertEqual(data["blocks"], 2)
            self.assertEqual(data["verdict"], "EDITABLE")
            self.assertEqual(data["version"], S.__version__)

    def test_diff_and_edit_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.txt", Path(d) / "b.txt"
            a.write_text('{"hp":10,"mana":5}', encoding="utf-8")
            b.write_text('{"hp":3,"mana":5}', encoding="utf-8")
            r = run_cli("diff", str(a), str(b), "--json")
            data = json.loads(r.stdout)
            self.assertEqual(len(data["changed"]), 1)
            self.assertEqual(data["changed"][0]["path"], "b0:hp")

            r = run_cli("edit", str(a), "--set", "b0:hp=3")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(a.read_text(encoding="utf-8")),
                             {"hp": 3, "mana": 5})
            # 自动备份存在且与原文一致
            baks = list(Path(d).glob("a.txt.bak-*"))
            self.assertEqual(len(baks), 1)
            self.assertEqual(json.loads(baks[0].read_text(encoding="utf-8")),
                             {"hp": 10, "mana": 5})

    def test_dry_run_leaves_file_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.txt"
            original = '{"hp":10}'
            p.write_text(original, encoding="utf-8")
            r = run_cli("edit", str(p), "--set", "b0:hp=1", "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(p.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(d).glob("*.bak-*")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
