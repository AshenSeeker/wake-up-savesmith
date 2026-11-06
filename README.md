# wake-up-savesmith

前阵子为了复活一个被误杀的 NPC，先把 GitHub 上扒素材、反编译游戏代码的工具翻了一圈，最后发现那次根本没用上。那游戏的存档就是个纯文本文件，三千多行，34 个 JSON 块用 `;` 和 `~SettingsSplit~` 拼在一起。真正费劲的是两件事：在这么多块里找出游戏到底在哪个字段记下了「这个 NPC 已经死了」，然后把它改回去，还不能顺手把文件改坏。

那次是一行行对出来的。改之前先把整个存档目录复制了一份，改完进游戏还得跟 Steam 云存档抢一下，选本地那份才没被覆盖回去。

这个脚本就是把那段里最机械、最容易翻车的两步做成命令：给两份快照找差异，以及安全地把某个值写回去。零第三方依赖，Python 3.9+，clone 下来直接跑。

有一点先说在前面：它不知道 `friendlyNPCsKilled` 是什么意思，也不会替你决定哪个字段该动。哪一处该改、改成多少，是人看出来的；脚本只管找得准，以及不改坏文件。

## 拿复活 NPC 那次走一遍

假设手上有两份存档，一份是 NPC 还活着的时候存的，一份是误杀之后存的。游戏每次存档都会顺手改一堆无关字段（游玩时长、坐标、背包顺序、存档时间戳），所以直接文本比对基本没法看。

先 `probe` 两份，确认都能读、块数一致：

```bash
python savesmith.py probe before.txt
python savesmith.py probe after.txt
```

它会报编码、BOM、字节数、sha256、块数、分隔符，以及有没有疑似校验和或签名的字段。那次的结果是 `EDITABLE`，34 个块，`~SettingsSplit~ x1, ; x32`，没有可疑键。

再 `diff`，把每次都会变的字段滤掉：

```bash
python savesmith.py diff before.txt after.txt --ignore playTime --ignore position --ignore timeAtLastSaveOrLoad
```

不滤的话那次报了 458 个变化，绝大多数是背包 GUID 重排和坐标漂移。滤完剩下真正要看的几处：

```
b3:questStage_Shopkeeper        999 -> 70
b3:storyProgressStage           16 -> 12
b3:friendlyNPCsKilled           1 -> 0 items  (-1: 90)
```

纯标量数组按「增删了哪些元素」报，不按下标，否则中间多一个元素后面会全被算成变化，全是噪音。路径里的 `b3` 是第 4 个 JSON 块，从 0 数起。

看到这三行，判断是人的活：任务状态被推到了失败，NPC 的编号被记进了已杀列表，剧情进度跟着退了一格。工具不替你下这个结论，它只把变化摆出来。

确认之后先 `--dry-run` 预览：

```bash
python savesmith.py edit after.txt \
  --set "b3:questStage_Shopkeeper=70" \
  --set "b3:friendlyNPCsKilled=[]" \
  --dry-run
```

校验全过、列出的改动正好是这两处，再去掉 `--dry-run` 真改。写盘前它会确认块数没变、每块之间的分隔文本没变、改动范围严格等于 `--set` 里那几条、每个值回读都等于给的新值；任何一条不过就不落盘。写的时候自动留一份带时间戳的备份，用原子替换，落盘后再读一遍字节比对。

最后进游戏看 NPC 回来没有。Steam 弹云存档冲突要选本地，选云端会把刚改的覆盖掉。不对就拿备份还原，别在改坏的文件上继续叠。

那次改完再用 `diff` 对回备份，结果只有这两处变化，其余 added / removed 全是 0。这就是脚本目前能替你做完的部分。

## 现在具体能做什么

**`probe`** 只读探一眼结构：编码、BOM、字节数、sha256、块数、分隔符、可疑校验和字段，给一个判断（`EDITABLE` / `CAUTION` / `UNREADABLE`）。`CAUTION` 表示探到疑似校验和或签名，直接改值可能让游戏拒读，`edit` 会拦住，除非明确加 `--force`；它不会替你重算校验和。`UNREADABLE` 一律拒改。

**`diff A B`** 按块索引对齐两份快照，列出变化的标量字段、数组元素的增删、以及新增或删除的键。`--ignore REGEX` 可重复，用来滤噪音；`--json` 输出给别的程序读。两份块数不一样会给警告，多出来的按增删报。

**`edit FILE --set PATH=VALUE`** 只替换目标值那一小段文本，不做整块 `json.dumps` 重写，所以 BOM、换行、缩进、其它字段的写法都原样保留。`--set` 可重复，`VALUE` 按 JSON 解析（`70`、`"foo"`、`true`、`null`、`[]`、`[1,2]`），解析不了就当字符串。省略块号（写 `questStage_Shopkeeper=70`）时会自动去找唯一含这个顶层键的块。只改已经存在的路径，不会增删键；路径找不到直接报错。`--backup PATH` 可以指定备份位置，默认生成 `SaveData.txt.bak-YYYYmmdd-HHMMSS`。

三档命令的详细参数看下面的速查。

## 它不碰的东西

别把「同一个引擎」当成「同一种存档」。同是 Unity，别的游戏可能用 BinaryFormatter、SQLite，或者给存档加签名、直接加密。这类文件 `probe` 会给 `UNREADABLE`，`edit` 拒绝，不会硬解它不认识的格式来显得能干。

字段语义也一样。`questStage_Shopkeeper=999` 是不是「任务失败」、`friendlyNPCsKilled` 里那个 90 是不是某个 NPC，这些得对着游戏本身去判断。工具不猜，猜错了改坏的是你的存档。

## 后面打算补的（3 / 4 / 5）

这次只做了当初拆出来的五件事里的 1（快照 diff）和 2（格式探测与安全闸门）。剩下三件留到后面，理由也简单：一次只加一样，才看得出来是哪样起了作用。

**3，字段词典。** 把「字段 → 含义 → 能改的范围」登记成一张可扩充的表。有了它，工具对登记过的游戏能直接说明某个键是干什么的，不用每次靠人临场认。代价是每加一个游戏都要人工核对语义，猜错就改坏存档，仓库也会从通用仪器变成半专用修改器。等真频繁撞上「diff 出来一堆键、不知道哪个该动」再上。

**4，工作流。** 现在的底子是下面那份 SOP，写成文档，不算脚本功能。往后想把它做厚：按具体游戏记下哪些字段是噪音、哪些改动会连带影响别处，做成一份能照着走的检查清单，而不是每次重新判断一遍。

**5，反查代码。** 存档里连字段名都看不出来的时候，才需要去编译后的 dll 里搜序列化类。那要接 dnSpy / ILSpy 这类外部工具，Mono 和 IL2CPP 还是两套做法。塞进来会毁掉现在最省心的一点：零依赖、clone 能跑、跨平台。所以先留着，等真遇到那种存档再说。

版本号停在 `0.1.0` 也是这个意思。它描述工具自己的命令和输出格式，不描述支持哪些游戏；字段词典和输出格式都还可能变，等稳了再谈 1.0。变更记在 [CHANGELOG.md](CHANGELOG.md)。

---

## 安装 / 运行

不用装，也不用 `pip install`，只用标准库。clone 下来直接跑主脚本：

```bash
python savesmith.py --version
python savesmith.py probe path/to/SaveData.txt
```

Windows 上 `python` 不在 PATH 就用解释器绝对路径：

```powershell
& "C:\path\to\python.exe" ".\savesmith.py" probe ".\SaveData.txt"
```

中文和特殊字符在部分 Windows 终端（pwsh 默认代码页）会显示成 `??`，那是终端编码的事，不是脚本问题；`--json` 输出和存档数据本身都是正常 UTF-8。

## 命令速查

```
probe FILE [--json]
diff A B [--ignore REGEX ...] [--json] [--max-list-items N]
edit FILE --set PATH=VALUE [--set ...] [--dry-run] [--backup PATH] [--force]
```

`probe` 输出示例：

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

`diff` 的路径写法：`b3:questStage_Shopkeeper`，数组元素 `b3:inv.items[0]`。

## 标准操作流程（SOP）

这套流程就是上面说的第 4 件的现状，照着走能把风险压到最低：

1. 先备份，别只依赖本工具。把游戏整个存档目录复制一份到别处；工具自动生成的备份是第二道保险。
2. `probe` 两份存档，确认都是 `EDITABLE`，块数和编码一致。看到 `CAUTION` 先停下来想清楚校验和怎么办。
3. `diff 改前 改后`，用 `--ignore` 滤掉每次都会变的字段，定位真正导致问题的那几条路径。
4. 判断语义，哪一处该改成什么、范围是多少。这一步是人的活。
5. `edit --dry-run` 预览，确认校验全过、列出的改动正是要改的那几处。
6. 真正 `edit`，记下它打印的备份文件名。
7. 进游戏验证。Steam 弹云存档冲突，选本地存档；选云端会把改动覆盖回去。
8. 不对就用备份还原，别在已经改坏的文件上继续叠改。

## 支持的存档形态

一个或多个顶层 JSON 值拼成的文本，块之间用 `~SettingsSplit~`、`;` 或换行分隔（Unity 常见写法）；单个 JSON 对象和 JSON Lines 也支持。按字节读写，保留 UTF-8、UTF-8-BOM、UTF-16-LE、UTF-16-BE 的 BOM 与换行。

二进制序列化、SQLite、带签名或加密、以及完全自定义的文本结构，都不在这套里，`probe` 会给 `UNREADABLE`，`edit` 拒绝。

## 真实数据与样例

仓库里不带任何真实存档。真实存档往往含账号标识和本机路径，素材本身也有版权，整档放上 GitHub 不合适。

`samples/` 是手工构造的脱敏小存档，用通用占位键名，没有账号、路径或版权信息，用来演示结构、跑 `diff` / `edit`、给 `tests/` 当夹具。字段含义见 [samples/README.md](samples/README.md)。

## 开发 / 测试

```bash
python -m unittest discover -s tests -v
```

16 个 unittest 覆盖扫描器、块切分、diff、edit 往返、安全闸门和编码保留。零第三方依赖，只用标准库（`json` / `re` / `pathlib` / `tempfile` / `hashlib` / `argparse` 等）。

## 许可

[MIT](LICENSE)。
