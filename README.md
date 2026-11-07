# fssp

FSSP is as known as Firing Squad Synchronization Problem. This repository is gathering implementations written in several programming languages (rust / golang / ruby).

耐故障 FSSP(Umeo 2004 の A1/A2/A3)の実装は rust 版が本体です。設計と論文との差異は
`Fault-Tolerant FSSP SPEC.md`、作業記録は `golang/MEMO.md` を参照してください。

## 必要なもの

- Rust(cargo)
- Python 3(標準ライブラリのみ。規則表の生成器とスイープに使う)
- Windows を前提にしたパスで書いていますが、`rust/freeze_tools.py` の `BIN` を
  変えれば他の OS でも同じです

## ビルド

```
cd rust
cargo build --release
```

実行ファイルは `rust\target\release\fssp.exe` です。以下のコマンドは `rust` ディレクトリで実行します。

## 規則表の生成(clone 後に 1 回)

耐故障用の規則表は大きい(200〜270 MB)ので git には入れず、リポジトリの基底
`balzer-freeze.rul.txt` から生成器で作ります。

```
python freeze_tools.py add_ab      # -> ..\balzer-freeze-full.rul.txt(従来モード用)
python pure_tools.py add_pure      # -> ..\balzer-freeze-pure.rul.txt(--pure 用、約 1 分)
```

| 規則表 | 用途 |
|---|---|
| `balzer.rul.txt` `mazoyer.rul.txt` `waksman-slim.rul.txt` | 故障なしの通常 FSSP |
| `balzer-freeze-full.rul.txt` | 故障あり、従来モード(A1/A2/A3) |
| `balzer-freeze-pure.rul.txt` | 故障あり、純粋シフト線モード `--pure`(A3 専用) |

## 実行例

故障なし(通常の FSSP、並列実行):

```
.\target\release\fssp.exe -r ..\waksman-slim.rul.txt -s 100
```

1 ステップごとの途中経過を端末に出す(`-c` は ANSI 色付き):

```
.\target\release\fssp.exe -r ..\balzer.rul.txt -s 20 -d
.\target\release\fssp.exe -r ..\balzer.rul.txt -s 20 -c
```

故障あり。`-f` に故障セル(1 始まり、`a-b` と `,` 区切り)を与えます。A1/A2 は
各正常区間の長さが隣の故障区間以上(n_i ≥ m_i)、A3(`--a3`)はさらに n_i + m_i ≥ p − i
が条件で、同期時刻は 2n−2(A3 は 2n−2+p)です。

```
.\target\release\fssp.exe -r ..\balzer-freeze-full.rul.txt -s 35 -f 14-15,24 --a3
.\target\release\fssp.exe -r ..\balzer-freeze-full.rul.txt -s 35 -f 14-15,24 --a3 -c
```

純粋シフト線モード(故障セルは左右の隣の状態を写すだけ。A3 専用、条件は n_i > m_i)。
規則表は pure 用を指定します:

```
.\target\release\fssp.exe -r ..\balzer-freeze-pure.rul.txt -s 35 -f 14-15,24 --a3 --pure
.\target\release\fssp.exe -r ..\balzer-freeze-pure.rul.txt -s 1000 -f 201-260,451-470,701-705,901 --a3 --pure
```

## ブラウザで見る(時空図ビューア)

`--html` で `-d -c` と同じ内容(各時刻の状態、故障セルの右行き線/左行き線、凍結、注記)を
自己完結の HTML に書き出します。ブラウザで開くと時刻の再生、ズーム、状態名での強調、
セルをクリックして 1 歩前の入力と遷移規則の鍵を確認、時空図全体の PNG / SVG 保存ができます。

```
.\target\release\fssp.exe -r ..\balzer-freeze-pure.rul.txt -s 1000 -f 201-260,451-470,701-705,901 --a3 --pure --html big.html
start big.html
```

`--json out.json` で同じ内容の JSON も出せます(`rust\viewer.html` を単体で開いてドロップすると表示されます)。

## スイープ(定理の検証)

条件を満たす配置を網羅して「全正常セルが 2n−2(+p) に同時発火、注入 0(A2 は p)」を確認します。
規則表は 1 回だけ読む一括モード(`--batch`)で回るので数秒〜20 秒です。

```
python freeze_tools.py sweep_a1    # A1: 305 配置
python freeze_tools.py sweep_a2    # A2: 487 配置
python freeze_tools.py sweep_a3    # A3: 1695 配置
python pure_tools.py sweep 1 1     # --pure A3: n_i >= m_i+1、最終区間 >= 1、p=1..4、n <= 16(1451 配置)
cargo test --release
```

## その他のオプション

```
.\target\release\fssp.exe -h
```

`--inject kind@cell@t`(制御メッセージの注入、デバッグ用)、`--wallset state@t`、
`-t`(ステップ上限)、`--strict`(条件を満たさない配置を拒否)、`--batch <file>` があります。
未定義の遷移鍵を調べるときは環境変数 `FSSP_TRACE_FB=1` を付けて実行します。
