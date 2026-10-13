# -*- coding: utf-8 -*-
# balzer を基礎に凍結・解凍をルール層で模倣する balzer-freeze.rul.txt の
# 生成(gen)と、正規実行との差分から不足する遷移規則を合成する(discover)ツール。
#
#   python -I freeze_tools.py gen        -> ../balzer-freeze.rul.txt を(再)生成
#   python -I freeze_tools.py discover   -> 差分合成ループ(全ケースで照合を閉じるまで)
#   python -I freeze_tools.py verify     -> 検証バッテリの結果レポート
#
# 設計(Fault-Tolerant FSSP SPEC.md「故障セル中継モデルの確定定義」+ Umeo 2004 Fig.3):
#   - 凍結変状態 F(x)(FB/FC/FR/FM ...)が凍結波を運ぶ: (l, c, F_d) -> F_c、(l, c, FS) -> F_c
#   - 凍結ホールド: (l, F_v, r) -> F_v(F_* / FS / FT / www が右隣の間)
#   - 解凍波はプレーンな状態が運ぶ: (F_l, F_v, p) -> f(l, v, p) 仮説で生成し、
#     差分合成で不足・不正な遷移を「ケース規則」として上書きする
#   - 外部信号(FS=凍結、FT=解凍)は右壁から提示(--wallset)。--wallset FS@T は
#     T ステップ目にセル n が信号を見る(= 論文の t1 に対し T = t1-1)
#   - 期待配置: セル x は行 f(x) = t1 + (n-x) から dt 行だけ凍結表示
#     (保持値 = golden[f(x)-1])、それ以前は golden[t]、以後は golden[t-dt]。
#     これは Umeo Fig.3 遅延版で行単位に確認した構造と一致する。
#   - 差異(設計方針): 初期セル(セル n)は論文の F5..F1 カウントダウン表示ではなく
#     F(held 値)で待機する(カウントダウンは固定 dt 実験の図版で、任意 dt には
#     有限状態で書けないため信号待機方式を採る)。
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(HERE, 'target', 'release', 'fssp.exe')
BASE_RULE = os.path.join(ROOT, 'balzer.rul.txt')
OUT_RULE = os.path.join(ROOT, 'balzer-freeze.rul.txt')

PAD = 5  # 状態名の桁幅(balzer.rul.txt と同一)

# 凍結されうる内部状態(www / 発火 F を除く)
INTERIOR = ['A', 'B', 'C', 'L', 'M', 'Q', 'R']
FVARS = ['F' + s for s in INTERIOR]
SIGNALS = ['FS', 'FT']


def pad(s):
    return s + ' ' * (PAD - len(s))


def load(path):
    with open(path, newline='') as f:
        lines = f.read().splitlines()
    states, rules, pre, post = [], [], [], []
    i = 0
    mode = 'pre'
    while i < len(lines):
        l = lines[i]
        if l.startswith('state_number'):
            pre.append(l)
            n = int(l.split(' ')[1])
            states = lines[i + 1:i + 1 + n]
            i += 1 + n
        elif l.startswith('rule_number'):
            n = int(l.split(' ')[1])
            rules = lines[i + 1:i + 1 + n]
            pre.append(l)
            i += 1 + n
            mode = 'post'
        else:
            (pre if mode == 'pre' else post).append(l)
            i += 1
    return states, rules, pre, post


def state_name(state_line):
    return state_line.split('@')[0].strip()


def parse_rules(lines):
    d = {}
    for r in lines:
        src, dst = r.split('->')
        key = tuple(t.strip() for t in src.split('##'))
        d[key] = dst.strip()
    return d


def emit(rulemap, keys):
    return [f'{pad(a)}##{pad(b)}##{pad(c)}->{pad(rulemap[(a, b, c)])}' for (a, b, c) in keys]


def write_file(states, pre, post, rule_lines):
    # rule_number 行の数値を書き換える
    out = []
    for l in pre:
        if l.startswith('state_number'):
            out.append(f'state_number {len(states)}')
        elif l.startswith('rule_number'):
            out.append(f'rule_number {len(rule_lines)}')
        else:
            out.append(l)
    text = '\r\n'.join(
        out[:2] + states + out[2:] + rule_lines + post) + '\r\n'
    with open(OUT_RULE, 'w', newline='') as f:
        f.write(text)


def gen():
    states, rules, pre, post = load(BASE_RULE)
    names = [state_name(s) for s in states]
    bmap = parse_rules(rules)

    # 状態一覧: 先頭に FB を置く(未登録遷移のフォールバック=凍結保持を安全側に)
    new_states = ['FB', 'A', 'B', 'C', 'F', 'L', 'M', 'Q', 'R', 'www',
                  'FA', 'FC', 'FL', 'FM', 'FQ', 'FR', 'FS', 'FT']
    assert set(new_states) == set(names) | set(FVARS) | set(SIGNALS)
    state_lines = [f'{pad(s)}@101010,808080,others' for s in FVARS]
    color_map = {'FS': ('FFFFFF', 'FF00FF'), 'FT': ('FFFFFF', '00EEEE')}
    signal_lines = [f'{pad(s)}@{color_map[s][0]},{color_map[s][1]},others'
                    for s in SIGNALS]
    by_name = {state_name(s): s for s in states}
    fvar_lines = [f'{pad(s)}@101010,808080,others' for s in FVARS]
    fm = dict(zip(FVARS, fvar_lines))
    full_states = (
        [fm['FB']]
        + [by_name[n] for n in ['A', 'B', 'C', 'F', 'L', 'M', 'Q', 'R', 'www']]
        + [fm[s] for s in ['FA', 'FC', 'FL', 'FM', 'FQ', 'FR']]
        + signal_lines
    )

    alls = new_states
    rulemap = dict(bmap)  # (l, c, r)-stripped -> target
    keys = lambda: list(rulemap.keys())

    # 波: FS から凍結開始
    for x in alls:
        for c in INTERIOR:
            rulemap[(x, c, 'FS')] = 'F' + c
    # 波: F_* から凍結伝播
    for x in alls:
        for c in INTERIOR:
            for d in FVARS:
                rulemap[(x, c, d)] = 'F' + c
    # 凍結ホールド(F_* / FS / www が右にある限り保持。FT は解凍信号なので
    # ホールドに含めない:イニシエータは FT の到着で解凍する)
    for x in alls:
        for v in INTERIOR:
            for r2 in ['FS', 'www'] + FVARS:
                rulemap[(x, 'F' + v, r2)] = 'F' + v
    # 仮説A: 凍結セルの解凍 (F_l, F_v, p) -> f(l, v, p)
    for l in INTERIOR:
        for v in INTERIOR:
            for p in INTERIOR:
                if (l, v, p) in bmap:
                    rulemap[('F' + l, 'F' + v, p)] = bmap[(l, v, p)]
    # 仮説A: 凍結左隣と共に走るアクティブセル (F_l, c, p) -> f(l, c, p)
    for l in INTERIOR:
        for c in INTERIOR:
            for p in INTERIOR:
                if (l, c, p) in bmap:
                    rulemap[('F' + l, c, p)] = bmap[(l, c, p)]

    ordered = emit(rulemap, keys())
    write_file(full_states, pre + [], post, ordered)
    print(f'balzer-freeze.rul.txt: states={len(full_states)} rules={len(ordered)}')


def run(binary, args):
    p = subprocess.run([binary] + args, capture_output=True, text=True, cwd=HERE)
    return p.stdout


def state_rows(text):
    skip = ('R:', 'L:', 'F:', 'cells:', 'segments:', 'constraint', 'note:',
            'time:', 'SYNCED', 'NOT')
    return [l.rstrip() for l in text.splitlines()
            if l.strip() and not l.startswith(skip)]


def cells(row):
    return row.split('|')


def golden_rows(n):
    return state_rows(run(BIN, ['-r', OUT_RULE, '-s', str(n), '-d']))


def expected_grid(gold, n, t1, dt):
    gold_c = [cells(row) for row in gold]
    rows = []
    for t in range(0, 2 * n - 2 + dt + 1):
        row = []
        for x in range(1, n + 1):
            f_ = t1 + (n - x)
            if t < f_:
                row.append(gold_c[t][x - 1])
            elif t < f_ + dt:
                row.append('F' + gold_c[f_ - 1][x - 1])
            else:
                row.append(gold_c[t - dt][x - 1])
        rows.append(row)
    return rows


CASES = [
    (11, 10, 5),   # Umeo Fig.3 遅延版と同条件
    (11, 5, 3),
    (11, 7, 1),
    (13, 9, 4),
    (15, 10, 6),
    (11, 3, 8),
    # 一般化検証用(discover の閉包確認: 新規規則が要らなければ規則集は
    # (n, t1, dt) に依存しない)
    (11, 10, 2),
    (11, 8, 4),
    (12, 6, 5),
    (13, 4, 7),
    (15, 7, 3),
    (17, 12, 7),
    (20, 9, 9),
    (20, 19, 4),
    (25, 11, 12),
]


def wall_state_at(t1, dt, t):
    # 右壁の提示状態: FT@t2-1、それ以前の [t1-1, t2-1) は FS、t1-1 より前は www
    t1p, t2p = t1 - 1, t1 + dt - 1
    if t < t1p:
        return 'www'
    if t < t2p:
        return 'FS'
    return 'FT'


def discover():
    states, rules, pre, post = load(OUT_RULE)
    rulemap = parse_rules(rules)
    discovered = {}  # ドライバが追加・修正した (規則キー, 目標値)
    added_total = 0
    for it in range(80):
        additions = {}
        for (n, t1, dt) in CASES:
            gold = golden_rows(n)
            act = state_rows(run(BIN, [
                '-r', OUT_RULE, '-s', str(n), '-d',
                '--wallset', f'FS@{t1 - 1}', '--wallset', f'FT@{t1 + dt - 1}']))
            exp = expected_grid(gold, n, t1, dt)
            # 最初の不一致行のみから規則を導出する。それ以前の行は正しい
            # 動的過程の上にあるので、導出した規則は正しい入力対照を持つ。
            for t, row in enumerate(act):
                if t >= len(exp):
                    print(f'  n={n} t1={t1} dt={dt}: ran past horizon (t={t})')
                    break
                ac, ec = cells(row), exp[t]
                mism = [(k, e.strip()) for k, (a, e) in enumerate(zip(ac, ec))
                        if a.strip() != e.strip()]
                if not mism:
                    continue
                if t == 0:
                    print(f'  n={n} t1={t1} dt={dt}: mismatch at t=0 ?!')
                    break
                prev = cells(act[t - 1])
                for (k, e) in mism:
                    c = k + 1
                    l = prev[k - 1].strip() if k > 0 else 'www'
                    right = (prev[k + 1].strip() if c < n
                             else wall_state_at(t1, dt, t - 1))
                    key = (l, prev[k].strip(), right)
                    additions.setdefault(key, e)
                break  # 最初の不一致行で止める
        if not additions:
            print(f'closure at iteration {it} (+{added_total} rules)')
            break
        flip = False
        for key, v in additions.items():
            if discovered.get(key) not in (None, v):
                print(f'FLIP-FLOP: {key} -> {v} (was {discovered[key]})')
                print('設計競合: 同一入力組み合わせに異なる遷移が必要。')
                flip = True
                continue
            if key not in discovered:
                added_total += 1
            discovered[key] = v
            rulemap[key] = v
        write_file(states, pre, post, emit(rulemap, list(rulemap.keys())))
        print(f'iteration {it}: +{len(additions)} proposed (total {added_total})')
        if flip:
            break
    else:
        print('did NOT close within 80 iterations')
    write_file(states, pre, post, emit(rulemap, list(rulemap.keys())))


def verify():
    ok = True
    for (n, t1, dt) in CASES:
        gold = golden_rows(n)
        act = state_rows(run(BIN, [
            '-r', OUT_RULE, '-s', str(n), '-d',
            '--wallset', f'FS@{t1 - 1}', '--wallset', f'FT@{t1 + dt - 1}']))
        exp = expected_grid(gold, n, t1, dt)
        bad = 0
        for t, row in enumerate(act):
            if t >= len(exp):
                bad += 1
                continue
            ac, ec = cells(row), exp[t]
            for a, e in zip(ac, ec):
                if a.strip() != e.strip():
                    bad += 1
        want = 2 * n - 2 + dt + 1
        fire_ok = len(act) == want
        status = 'OK' if (bad == 0 and fire_ok) else 'NG'
        if status == 'NG':
            ok = False
        print(f'n={n} t1={t1} dt={dt}: {status} (mismatched={bad}, '
              f'fire={len(act) - 1} want={want - 1}, first-fire={fire_ok})')
    print('ALL OK' if ok else 'FAILURES PRESENT')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'verify'
    {'gen': gen, 'discover': discover, 'verify': verify}[cmd]()