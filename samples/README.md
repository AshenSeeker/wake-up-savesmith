# 样例说明

这里的文件是**手工构造的脱敏样例**，不是任何真实游戏的存档。
键名（questStage_Guide、friendlyNPCsKilled、storyProgressStage …）只是用来演示
「一段由 `;` + `~SettingsSplit~` 拼接的 JSON 文本块」长什么样，不含账号标识、
本机绝对路径、版权素材，也不对应任何真实游戏的数据定义。

- `before.txt` — 「改前」快照：4 个 JSON 块，`playTime` 较小，任务阶段 50，列表为空。
- `after.txt` — 「改后」快照：结构相同，但 `playTime`、任务阶段（999）、列表（多了 90）、进度阶段（16）变了。

试着跑：

```bash
python ..\savesmith.py probe before.txt
python ..\savesmith.py diff before.txt after.txt --ignore playTime
python ..\savesmith.py edit before.txt --set "b2:questStage_Guide=50" --dry-run
```

真实存档格式核实过：某 Unity Mono 游戏存档是 3211 行、34 个块、
`~SettingsSplit~` ×1 + `};{` 分隔，utf-8 no BOM——但那份文件**没有**放进本仓库，
理由见主 README「真实数据与样例」一节。
