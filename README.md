# osu2simai

将 **osu!mania 4K** 键盘谱转换为 maimai DX 风格的 **纯 touch 谱**（simai 文本 + JSON 内部格式）的小工具。

- osu 单键 → `touch`（E6/B5/B4/E4 等区域）
- osu 长条 → `touchhold`（`h[division:beats]`，时长量化误差 ≤ ~1.3ms）
- 4 列键盘（DFJK / 列 0..3）默认映射为 `E6, B5, B4, E4`，可用 `--keymap` 修改

纯标准库，**无第三方依赖**，需要 Python 3.10+。

## 生成产物

每个谱面生成到自己的文件夹 `<输出目录>/<谱面名>/` 里，主文件是**固定文件名**：

```
output/
└── Saikoro - far in the blue sky... (1nar) [42]/
    ├── maidata.txt   ← simai 谱面（开头带 & 元数据头，工具需要）
    └── chart.json    ← JSON 谱面（touches[] / toucheHolds[]）
```

`--extras` 会在同一文件夹里额外生成：

| 文件 | 说明 |
| --- | --- |
| `fumen.txt` | 裸 simai 正文（无元数据头，供只需要谱面内容的解析器） |
| `expected_notes.csv` | 与 osu 原谱逐条对应的参考数据（自检用） |

批量转换时每个难度各自一个文件夹，互不覆盖。

## 用法

每次转换**必须**指定难度档 `--tier` 和等级数字 `--num`（见下节）：

```bash
# 单个谱面
python osu2simai.py "Saikoro - far in the blue sky... (1nar) [42].osu" \
    --tier master --num 42

# 批量转换（同一档位/等级应用到所有文件，各自成文件夹）
python osu2simai.py "map folder/"*.osu --tier advanced --num 8

# 自定义输出根目录 / 键位映射 / 额外生成裸谱与参考 csv
python osu2simai.py song.osu -o out/ --tier utage --num 14.7 \
    --keymap "E6,B5,B4,E4" --extras
```

常用参数（`python osu2simai.py -h` 查看全部）：

| 参数 | 说明 |
| --- | --- |
| `-t, --tier` | **必填**，难度档（下表 7 档的精确名字，无别名） |
| `-n, --num` | **必填**，等级数字，必须 > 0（如 `42`、`14.7`） |
| `-o/--output-dir` | 输出根目录（默认 `output`，其下按谱面名分子文件夹） |
| `--keymap` | 每列一个 touch 区域（默认 `E6,B5,B4,E4`） |
| `--bpm` | 覆盖 osu 计时点的 BPM（仅用于多 BPM 谱面强制） |
| `--first` | 谱面偏移 ms（`&first` / JSON `offset`） |
| `--charter` | 谱师名（默认 osu Creator） |
| `--formats` | `all`（maidata.txt + chart.json）/ `maidata` / `json` / `fumen`（只写裸正文） |
| `--extras` | 额外写 `fumen.txt` 与 `expected_notes.csv` |
| `--quiet` | 只输出错误 |

## 难度档（--tier）与等级（--num）

目标格式没有"自动猜等级"：每次都要显式给档位和数字。**档位只接受下表
7 个精确写法（小写、无别名）**，`expect`、`宴会场` 等其它拼写都会被拒绝。

| 档位（精确写法） | 含义 | maidata 槽位 | JSON `difficulty` |
| --- | --- | --- | --- |
| `easy` | EASY | 1 | 0 |
| `basic` | BASIC | 2 | 1 |
| `advanced` | ADVANCED | 3 | 2 |
| `expert` | EXPERT | 4 | 3 |
| `master` | MASTER | 5 | 4 |
| `remaster` | Re:MASTER | 6 | 5 |
| `utage` | 宴会场 | 7 | 6 |

注意两处编号不同：maidata 槽位 `inote_<slot>`/`lv_<slot>` 用 **1..7**；
JSON 的 `notes[].difficulty` 用 **0..6**（`level` 字段才是等级数值）。

`--num` 必须是大于 0 的数字，写入 `lv_<slot>`（maidata）与 JSON `level`
（如 `42` 写成 `"42"`，`14.7` 写成 `"14.7"`）。

槽位/难度编号顺序如果想调整，改脚本顶部 `TIER_ORDER` 即可。

## 时间编码

- **simai**：`(BPM)` 起始，`{16}` 网格，1 个逗号 = 1/16 拍；每行恰好一个小节（16 槽），谱面以单独一行 `E` 结尾（与 MaichartConverter 输出风格一致）。
- **JSON**：`{split, beat}` 表示 `beat/split × 1 小节`（1 小节 = 4 拍）。所有 hitTime/holdTime 均输出最简分数；hold 时长与 simai 的 `[division:beats]` 一一对应。

## 限制

- 仅支持 **单一 BPM** 的 osu!mania 谱面（多 BPM 会报错，可用 `--bpm` 强制指定但时间可能错位）。
- 列数必须与 `--keymap` 数量一致（默认 4 列）。
- 音符按 1/16 网格吸附（偏差 >4ms 会给出警告）。

## 校验

`--extras` 生成的 `expected_notes.csv` 是与 osu 原谱逐条对应的参考数据；
转换器内置自检保证 simai/JSON 的音符数、键位、类型与它一致。开发期还使用
[MajSimai](https://github.com/LingFeng-bbben/MajSimai) 做过端到端回读验证（3167/3167 匹配）。

## 致谢 / Credits

- simai 谱面布局风格参考 [MaichartConverter (Neskol)](https://github.com/Neskol/MaichartConverter) 输出
- 语法验证基于 [MajSimai (Majdata)](https://github.com/LingFeng-bbben/MajSimai)

## License

[MIT](./LICENSE)
