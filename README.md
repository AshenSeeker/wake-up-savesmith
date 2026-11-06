# wake-up-savesmith

> 给「明文半结构化存档」做的**两份快照之间找状态差异 + 安全地把某个值改回去**的小工具。
> 零第三方依赖，Python 3.9+，clone 下来就能跑。

**它不认识任何游戏，不判断字段语义，不复活 NPC。**
`friendlyNPCsKilled` 是不是「杀过的友好 NPC」、`questStage_Shopkeeper=999` 是不是「任务已失败」——
这些判断是**你**下的。工具只负责两件事：**找得准**，以及**不改坏文件**。

---

## 为什么是这么个东西

很多 Unity 游戏的存档是「一堆 JSON 值拼接成的文本文件」，块之间用 `;`、`~SettingsSplit~` 或换行分隔。
出问题时（误杀 NPC、丢了唯一道具、任务状态卡死），你手上往往有改前和改后的存档，或者愿意先存一份再动。
真正繁琐又容易翻车的是两步：

1. 在几千行、几十个拼接块里**人肉对出到底是哪个字段变了**；
2. 把它改回去，且**别把文件改坏**（少个分隔符、编码变了、顺手带崩别的值）。

这个脚本把这两步自动化了。它**通用**：只要是同一套「JSON 值拼接文本」结构就能用，不绑引擎、不绑游戏、不绑「复活」这个单一用例。
但也正因为通用，**语义判断留给人**——这是它不猜错、不坑人的原因。

> ⚠️ 引擎相同 ≠ 存档相同。同是 Unity，别的游戏可能用 BinaryFormatter、SQLite、带签名甚至加密。
> 本工具对那些文件会直接 `UNREADABLE` 并拒绝改写，而不是硬来。`probe` 先看一眼就知道能不能用。

---

## 它能做什么（1 + 2）

| 能力 | 命令 | 输出 |
|---|---|---|
| **格式探测 + 风险闸门** | `probe` | 编码/BOM、字节数、sha256、块数、分隔符、可疑校验和字段、verdict |
| **快照 diff** | `diff A B` | 变化字段路径 + 原值/现值、数组增减、增删键；可 `--ignore` 过滤噪音、`--json` 机器读 |
| **安全改写** | `edit` | 只替换目标值的那一小段文本，带备份/原子写/回读校验 |

**没做、且刻意没做的**：字段词典（把字段→含义登记成表）、工作流文档以外的教程、反编译 dll 反查代码。
前两者会让仓库从「通用仪器」变成「半专用修改器」，后者会毁掉零依赖、clone 即用、跨平台的属性。
真遇到「diff 出来一堆键、不知道哪个该动」再按需加，那时加也不迟。

---

## 安装 / 运行

不需要安装，也不需要 `pip install`。把仓库 clone 下来，直接用 Python 跑主脚本：

```bash
python savesmith.py --version
python savesmith.py probe path/to/SaveData.txt
```

Windows 上若 `python` 不在 PATH，用解释器的绝对路径即可：
```powershell
& "C:\path\to\python.exe" ".\savesmith.py" probe ".\SaveData.txt"
```

> 控制台里中文/特殊字符在部分 Windows 终端（pwsh 默认代码页）下会显示成 `??`，
> 那是终端编码问题，不是脚本 bug；`--json` 输出和数据本身都是正常的 UTF-8。

---

## 命令速查

### `probe` —— 先探查，别上来就改
```bash
python savesmith.py probe SaveData.txt
python savesmith.py probe SaveData.txt --json
```
示例输出：
```
wake-up-savesmith 0.1.0 — probe
  file:       SaveData.txt
  size:       127965 bytes
  encoding:   utf-8 (no BOM)
  sha256:     adc78504…
  format:     multi-json-blocks (34 blocks)
  delimiters: ~SettingsSplit~ x1, ; x32
  parse:      all blocks parse as JSON
  suspicious keys: none
  verdict:    EDITABLE  (diff / edit supported)
```
verdict 三档：
- `EDITABLE` — 可以 diff / edit。
- `CAUTION` — 探到疑似校验和/签名字段（如 `checksum`、`signature`），直接改值可能让游戏拒读；`edit` 会拒绝，除非你明确 `--force`。**它不会帮你重算校验和。**
- `UNREADABLE` — 解析不出 JSON 块，`edit` 一律拒绝。

### `diff` —— 两份快照找差异
```bash
python savesmith.py diff before.txt after.txt
python savesmith.py diff before.txt after.txt --ignore playTime --ignore position
python savesmith.py diff before.txt after.txt --json
```
- 按**块索引**对齐（两份存档块数不同会给警告，多出来的按增删报）。
- `--ignore REGEX` 可重复，按字段路径正则过滤噪音字段（游戏每次存档都会变的 `playTime`、坐标、`timeAtLastSaveOrLoad` 之类）。
- 纯标量数组按「增/删了哪些元素」报，不按下标——否则中间插一个元素会把后面全报成变化，全是噪音。
- 路径形如 `b3:questStage_Shopkeeper`（`b3` = 第 4 个 JSON 块，从 0 数）、数组元素 `b3:inv.items[0]`。

### `edit` —— 安全改写
```bash
# 先 dry-run 预览，只校验不写盘
python savesmith.py edit SaveData.txt --set "b3:questStage_Shopkeeper=70" --dry-run

# 确认无误，真正写
python savesmith.py edit SaveData.txt --set "b3:questStage_Shopkeeper=70" --set "b3:friendlyNPCsKilled=[]"

# 省略块号：自动定位「唯一含该顶层键」的块
python savesmith.py edit SaveData.txt --set "questStage_Shopkeeper=50"
```
- `--set PATH=VALUE` 可重复；`VALUE` 按 **JSON** 解析（`70`、`"foo"`、`true`、`null`、`[]`、`[1,2]`），解析不了就当字符串。
- 只改**已存在**路径的值，不会新增/删除键；路径找不到会报错而不是乱猜。
- `--backup PATH` 指定备份位置；默认自动生成 `SaveData.txt.bak-YYYYmmdd-HHMMSS`（同名再加 `-N`）。

**写盘前的全部校验（任一不过，绝不落盘）**：
1. 块数不变；
2. 每个块的分隔文本（`gap`）不变；
3. 改动后的「扁平文档差异集合」必须**恰好等于** `--set` 的允许集合（防止误改到别的值）；
4. 逐项回读 token，值必须等于你给的新值。

落盘后还会再读一遍磁盘字节，确认和验证过的那份完全一致。备份、原子写（`os.replace`）、权限保留都在这一步完成。

---

## 标准操作流程（SOP）

这套流程本身就是「第 4 件」——写成文档，不算脚本的新功能。照着走能把风险压到最低：

1. **备份，且别只依赖本工具**。先把游戏整个存档目录复制一份到别处；本工具的自动备份是第二道保险，不是第一道。
2. **`probe` 两份存档**，确认都是 `EDITABLE`、块数一致、编码一致。看到 `CAUTION` 先停下来想清楚校验和怎么办。
3. **`diff 改前 改后`**，配合 `--ignore` 把每次存档都变的字段滤掉，定位真正导致问题的那几个字段路径。
   > 真实存档里噪音很多：背包 GUID 重排、`position`/`rotation`、`visibilityBitMask`、`timeAtLastSaveOrLoad`。
   > 一次真实 diff 报出 458 个变化，其中只有 `questStage_Shopkeeper 999→70`、`friendlyNPCsKilled -[90]`、
   > `storyProgressStage 16→12` 是真正要看的——`--ignore` 不是可选项，是必要步骤。
4. **判断语义**（这一步是人的活）：哪个字段该改成什么、能改的范围是多少。工具不替你做这个判断。
5. **`edit --dry-run`** 先预览：确认 `checks` 全 OK、`edits` 列出的正是你要改的那几个值。
6. **真正 `edit`**，记下它打印的备份文件名。
7. **进游戏验证**。Steam 若弹「云存档冲突」，**务必选本地存档**让它用你改过的那份；选云端会把改动覆盖回去。
8. 不对就**用备份还原**，别在已经改坏的文件上继续叠改。

---

## 支持的存档形态

一个或多个顶层 JSON 值拼成的文本，块之间用 `~SettingsSplit~`、`;` 或换行分隔（Unity 常见写法）；
也支持单个 JSON 对象和 JSON Lines（每行一个对象）。保留 UTF-8 / UTF-8-BOM / UTF-16-LE / UTF-16-BE 的 BOM 与换行，按字节读写。

**不是这套结构的**（二进制、二进制序列化、SQLite、带签名/加密、完全自定义文本）——`probe` 给 `UNREADABLE`，`edit` 拒绝。
工具不会为了「显得能干」去硬解它不认识的格式。

游戏兼容性清单不写进版本号：版本（`0.1.0`）描述的是工具自身的 API 和输出格式，不描述「支持哪些游戏」。

---

## 真实数据与样例

本项目**不附带任何真实存档**。真实存档往往含账号标识、本机绝对路径，且素材本身有版权，整档上 GitHub 不合适。

`samples/` 里放的是**脱敏的、手工构造的**小存档（通用占位键名、无账号/路径/版权信息），
用来演示结构、跑 `diff`/`edit`、以及给 `tests/` 当夹具。字段含义见 `samples/README.md`。

---

## 开发 / 测试

```bash
python -m unittest discover -s tests -v
```
零第三方依赖，只用标准库（`json` / `re` / `pathlib` / `tempfile` / `hashlib` / `argparse` …）。

---

## 版本

当前 `0.1.0`。`0.x` 是故意的：字段词典和输出格式都还可能变，等稳了再谈 1.0。
版本号只跟工具本身走，别跟「支持哪些游戏」绑定。变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

[MIT](LICENSE)。
