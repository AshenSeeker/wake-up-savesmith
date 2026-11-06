# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version numbers describe the **tool's** API and output format, NOT which games
are supported. Game-specific knowledge (if ever added) lives in a separate
document; it does not bump the tool version by itself.

## [0.1.0] - 2026-11-06

First release. Scope: features **1 (snapshot diff)** and **2 (format probe +
safe edit)** from the original plan.

### Added
- `probe` — read-only structure report: encoding/BOM, byte size, sha256,
  number of concatenated JSON blocks, delimiters, and a verdict
  (`EDITABLE` / `CAUTION` / `UNREADABLE`). Flags possible integrity fields
  (checksum / signature) by whole-word camelCase matching.
- `diff A B` — align two snapshots by block index and report changed scalar
  fields, array item additions/removals, and added/removed keys. `--ignore REGEX`
  filters volatile noise (e.g. `playTime`); `--json` for machine output.
- `edit FILE --set PATH=VALUE` — surgical replacement of only the target value
  token (no whole-file `json.dumps` rewrite). `--dry-run` previews without writing.
- Safety gates before any write: block count unchanged, every block separator
  unchanged, the set of changed flat paths equals exactly the `--set` allow-list,
  and per-field readback equality. Auto timestamped backup (`.bak-YYYYmmdd-HHMMSS`),
  atomic write via `os.replace`, and a post-write byte re-read.
- Handles UTF-8 / UTF-8-BOM / UTF-16-LE / UTF-16-BE, preserving BOM and line
  endings byte-for-byte. Zero third-party dependencies, Python 3.9+.
- `tests/test_savesmith.py` — unittest suite covering the scanner, block split,
  diff, edit round-trip, safety gates, and encoding preservation.
- `samples/` — de-identified synthetic saves plus a README explaining that no
  real save is shipped.

### Notes
- The tool is game-agnostic on purpose. It never guesses what a field *means*
  and does not add or remove keys — it only sets values for paths that exist.
- A `CAUTION` verdict (suspected checksum/signature) requires `--force` to edit.
  That does not recompute the checksum; it only acknowledges the risk.
