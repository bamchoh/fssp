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


def add_a1():
    # A1/A2 接続用の付加。既存の発見規則を保持したまま追記する(regen はしない)。
    # - NG: 将軍起動信号(管路が I_{i+1} 左端の左入力に提示)
    # - W : 管路の境界マーカー(R→L 管路のアイドル提示。www と別の状態にして
    #       凍結起動トリガー (C,L,W)->FL を素の壁反射 (C,L,www)->M と区別する)
    # - FT2: 解凍後位相のアイドル提示(凍結を経た区間の右端では、σ 線上の
    #       壁反射 (C,L,FT2)->M は初回の凍結起動と区別されて正しく再発火する)
    states, rules, pre, post = load(OUT_RULE)
    rulemap = parse_rules(rules)
    names = [state_name(s) for s in states]
    if 'NG' not in names:
        states.append(f'{pad("NG")}@101010,808080,others')
        names.append('NG')
    if 'W' not in names:
        states.append(f'{pad("W")}@101010,404860,others')
        names.append('W')
    if 'FT2' not in names:
        states.append(f'{pad("FT2")}@101010,7090E0,others')
        names.append('FT2')
    # CB: 境界Carrier(fast信号の管路引き渡し)。凍結起動トリガーのステップだけ
    # 境界セルが表示する。左隣からは F(held) と同じに見え、右側には信号を流す。
    if 'CB' not in names:
        states.append(f'{pad("CB")}@101010,0088CC,others')
        names.append('CB')
    additions = [
        (('www', 'L', 'L'), 'L'),    # 区間左端のアイドル(balzer の www 規則にはない)
        (('www', 'L', 'www'), 'L'),  # 1 セル区間(右端)のアイドル
        (('www', 'L', 'W'), 'L',),   # 1 セル中間区間のアイドル(左=www、右=管路 W)
        (('NG', 'L', 'L'), 'M'),     # 管路の将軍起動信号提示でセルを M へ
        # 起動区間が右端(長さ1)の場合は起動=発火((NG, L, www) のみが目印)
        (('NG', 'L', 'www'), 'F'),
        # n1=1 の縮退区間: FS クランプ時の将軍が FM で凍結し、FT 到着=発火
        # (2n1-2 = 0 なので解凍そのものが射撃。非縮退では FT はセル1の入力に現れない)
        (('www', 'FM', 'FT'), 'F'),
        # A2: 1 セルの中間区間は起動(NG, 左入力)と凍結(FS, 右入力)が同一ステップ
        # に同時提示される(n_i >= 2 では FS が後発なのでこの組み合わせは出ない)
        (('NG', 'L', 'FS'), 'FL'),
        # A2: 1 セル中間区間の解凍=発火(凍結保持値は L、左入力は管路の www)
        (('www', 'FL', 'FT'), 'F'),
        # 凍結起動トリガー: 区間の自らの構成の C 波が右端に到達したステップで
        # 壁反射の代わりに凍結に移る。トリガーはこのステップだけ使われる。
        # トリガーの遷移先は CB(= F(held) + fast信号の継続を担う境界Carrier)。
        (('L', 'L', 'W'), 'L'),
        (('C', 'L', 'W'), 'CB'),
        # 区間の長さが 2 のときは C 波の最初の到達セルの左隣が M
        (('M', 'L', 'W'), 'CB'),
        # CB の次状態: 凍結表示 F(L) へ(凍結窓の開始は t_i1+1 ではなく t_i1 の
        # ままにする: CB は同じステップ内の carrier として働く)
        (('C', 'CB', 'W'), 'FL'),
    ]
    n_added = 0
    for key, target in additions:
        if rulemap.get(key) != target:
            if key in rulemap:
                print(f'  override {key}: {rulemap[key]} -> {target}')
            rulemap[key] = target
            n_added += 1
    # 凍結ホールドの W 版: (x, F_v, www) -> F_v の規則を (x, F_v, W) にも複製する
    n_hold_w = 0
    for key in list(rulemap.keys()):
        if (key[2] == 'www' and len(key[1]) == 2 and key[1][0] == 'F'
                and key[1][1] in 'ABCLMQR'):
            new_key = (key[0], key[1], 'W')
            if rulemap.get(new_key) != rulemap[key]:
                rulemap[new_key] = rulemap[key]
                n_hold_w += 1
    # 境界Carrier CB: セルが CB を表示している間、その左(凍結側)のルールは
    # これを F(L)=FL と同じに扱う。(x, FL, r) -> y の規則を (x, CB, r) にも複製。
    cb_copies = 0
    for key in list(rulemap.keys()):
        if key[1] == 'FL':
            new_key = (key[0], 'CB', key[2])
            if rulemap.get(new_key) != rulemap[key]:
                rulemap[new_key] = rulemap[key]
                cb_copies += 1
    # 解凍後位相 FT2: (x, a, www) -> y の規則を (x, a, FT2) -> y にも複製する。
    # FT2 は右端の管路だけが提示するので、境界セル以外の既存規則には影響しない。
    ft2_copies = 0
    for key in list(rulemap.keys()):
        if key[2] == 'www':
            new_key = (key[0], key[1], 'FT2')
            if rulemap.get(new_key) != rulemap[key]:
                rulemap[new_key] = rulemap[key]
                ft2_copies += 1
    ordered = emit(rulemap, list(rulemap.keys()))
    write_file(states, pre, post, ordered)
    print(f'add_a1: states={len(states)} rules={len(ordered)} '
          f'(+{n_added}+{n_hold_w}W-hold+{ft2_copies}FT2+{cb_copies}CB)')


def run(binary, args):
    p = subprocess.run([binary] + args, capture_output=True, text=True, cwd=HERE)
    return p.stdout


def state_rows(text):
    skip = ('R:', 'L:', 'F:', 'cells:', 'segments:', 'constraint', 'note:',
            'a1:', 'time:', 'SYNCED', 'NOT')
    return [l.rstrip() for l in text.splitlines()
            if l.strip() and not l.startswith(skip)]


def stream_rows(text):
    # (R行, L行) の時系列。故障セルだけ [名前] を持つ。
    r, l = [], []
    for line in text.splitlines():
        s = line.rstrip()
        if s.startswith('R:'):
            r.append(s)
        elif s.startswith('L:'):
            l.append(s)
    return r, l


def srow_cells(row):
    # |区切りの列。故障セルだけ [name]、正常セルは空白。
    return [c.strip() for c in row.split('|')[1:-1]]


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


# ── A1 接続(p=1)の検証 ────────────────────────────────────────────────
# ケースは (n1, m1, n2)。期待配置:
#   - I1 (セル 1..n1): t1 = n1-1 から Δt = 2m1+2n2 行の凍結
#     (cell x: f = t1+(n1-x)。t<f なら gold1[t]、f<=t<f+Δ なら F(gold1[f-1])、
#      f+Δ <= なら gold1[t-Δ])
#   - D1 (セル n1+1..n1+m1): 静止表示 'L'
#   - I2 (セル n1+m1+1..): t >= 2(n1+m1) で gold2[t - 2(n1+m1)]、それ以前 'L'
A1_CASES = [
    (13, 1, 6),   # n=20 -f 14 (実際の症例)
    (5, 1, 5),
    (5, 2, 4),
    (6, 3, 4),
    (5, 1, 9),    # n2 > n1+m1
    (8, 4, 5),
    (10, 5, 10),
    (2, 1, 2),
    (3, 3, 4),
]


def a1_expected(gold1_c, gold2_c, n1, m1, n2):
    n = n1 + m1 + n2
    t1 = n1 - 1
    dt = 2 * m1 + 2 * n2
    T2 = 2 * (n1 + m1)
    rows = []
    for t in range(0, 2 * n - 2 + 1):
        row = []
        for x in range(1, n + 1):
            if x <= n1:
                f_ = t1 + (n1 - x)
                if t < f_:
                    row.append(gold1_c[t][x - 1])
                elif t < f_ + dt:
                    if x == n1 and t == f_ and n1 >= 2:
                        row.append('CB')  # 境界の1ステップCarrier
                    else:
                        row.append('F' + gold1_c[f_ - 1][x - 1])
                else:
                    row.append(gold1_c[t - dt][x - 1])
            elif x <= n1 + m1:
                row.append('L')
            else:
                x2 = x - (n1 + m1)
                row.append(gold2_c[t - T2][x2 - 1] if t >= T2 else 'L')
        rows.append(row)
    return rows


def run_a1(n1, m1, n2):
    n = n1 + m1 + n2
    spec = f'{n1 + 1}' if m1 == 1 else f'{n1 + 1}-{n1 + m1}'
    text = run(BIN, ['-r', OUT_RULE, '-s', str(n), '-d', '-f', spec])
    return state_rows(text), text


def verify_a1():
    ok = True
    for (n1, m1, n2) in A1_CASES:
        n = n1 + m1 + n2
        gold1_c = [cells(row) for row in golden_rows(n1)]
        gold2_c = [cells(row) for row in golden_rows(n2)]
        exp = a1_expected(gold1_c, gold2_c, n1, m1, n2)
        act, _ = run_a1(n1, m1, n2)
        bad = 0
        first = None
        for t, row in enumerate(act):
            if t >= len(exp):
                bad += 1
                continue
            ac, ec = cells(row), exp[t]
            for k, (a, e) in enumerate(zip(ac, ec)):
                if a.strip() != e.strip():
                    bad += 1
                    if first is None:
                        first = (t, k + 1, a.strip(), e.strip())
        want = 2 * n - 2 + 1
        fire_ok = len(act) == want
        status = 'OK' if (bad == 0 and fire_ok) else 'NG'
        if status == 'NG':
            ok = False
        print(f'a1 n1={n1} m1={m1} n2={n2}: {status} (mismatched={bad}, '
              f'fire={len(act) - 1} want={want - 1})'
              + (f' first t={first[0]} cell{first[1]} act={first[2]} exp={first[3]}'
                 if first is not None else ''))
    print('ALL OK' if ok else 'FAILURES PRESENT')


def discover_a1():
    states, rules, pre, post = load(OUT_RULE)
    rulemap = parse_rules(rules)
    discovered = {}
    added_total = 0
    A1_CASES_LOCAL = list(A1_CASES)
    for it in range(120):
        additions = {}
        for (n1, m1, n2) in A1_CASES_LOCAL:
            n = n1 + m1 + n2
            gold1_c = [cells(row) for row in golden_rows(n1)]
            gold2_c = [cells(row) for row in golden_rows(n2)]
            exp = a1_expected(gold1_c, gold2_c, n1, m1, n2)
            act, text = run_a1(n1, m1, n2)
            rr, ll = stream_rows(text)
            rrows = [srow_cells(r) for r in rr]
            lrows = [srow_cells(l) for l in ll]
            for t, row in enumerate(act):
                if t >= len(exp):
                    continue
                ac, ec = cells(row), exp[t]
                mism = [(k, e.strip()) for k, (a, e) in enumerate(zip(ac, ec))
                        if a.strip() != e.strip()]
                if not mism or t == 0:
                    continue
                # 不一致行の直前のアクティブ行+管路提示から規則キーを組み立てる
                prev = cells(act[t - 1])
                rrow = rrows[t - 1] if t - 1 < len(rrows) else None
                lrow = lrows[t - 1] if t - 1 < len(lrows) else None
                for (k, e) in mism:
                    ck = k + 1  # 1 始まりセル番号
                    if ck == 1:
                        l = 'www'
                    elif rrow is not None and rrow[ck - 2].startswith('['):
                        l = rrow[ck - 2][1:-1]
                    else:
                        l = prev[k - 1].strip()
                    if ck == n:
                        r = 'www'
                    elif lrow is not None and lrow[k + 1].startswith('['):
                        r = lrow[k + 1][1:-1]
                    else:
                        r = prev[k + 1].strip()
                    key = (l, prev[k].strip(), r)
                    additions.setdefault(key, e)
                break  # 最初の不一致行のみ
        if not additions:
            print(f'A1 closure at iteration {it} (+{added_total} rules)')
            break
        flip = False
        for key, v in additions.items():
            if discovered.get(key) not in (None, v):
                print(f'FLIP-FLOP: {key} -> {v} (was {discovered[key]})')
                flip = True
                continue
            if key not in discovered:
                added_total += 1
                print(f'  + ({key[0]}, {key[1]}, {key[2]}) -> {v}')
            discovered[key] = v
            rulemap[key] = v
        write_file(states, pre, post, emit(rulemap, list(rulemap.keys())))
        print(f'A1 iteration {it}: +{len(additions)} proposed (total {added_total})')
        if flip:
            break
    write_file(states, pre, post, emit(rulemap, list(rulemap.keys())))


# ── A2 (p>=2) の配列配置列挙スイープ ──────────────────────────────────
def sweep_a2():
    # p=2: n=5..13 の全配置 (n_i >= m_i、区間は接触不許可=統合済み扱い)。
    # p=3, p=4: 代表配置。期待 = SYNCED at 2n-2 (Theorem 3)。
    import itertools

    def a2_run(n, spec):
        return run(BIN, ['-r', OUT_RULE, '-s', str(n), '-f', spec])

    def check(n, spec, segs, fails):
        fails[1] += 1
        out = a2_run(n, spec)
        if f'SYNCED (intact cells) at t={2 * n - 2}' not in out:
            fails[0] += 1
            print(f'FAIL n={n} -f {spec} (n/m={segs})')
            for line in out.splitlines():
                if 'SYNCED' in line or 'NOT' in line:
                    print('   ', line.strip())
        # A3 表示の有無を確認する(失敗でなく記録のみ)
        return 'constraint n_i+m_i>=p-i (A3): FAIL' in out

    fails = [0, 0]
    a3_fail = 0
    for n in range(5, 14):
        for a1 in range(2, n - 1):
            for b1 in range(a1, n - 1):
                for a2 in range(b1 + 2, n):
                    for b2 in range(a2, n):
                        if b2 >= n:
                            continue
                        n1, m1 = a1 - 1, b1 - a1 + 1
                        n2, m2 = a2 - b1 - 1, b2 - a2 + 1
                        if n1 < m1 or n2 < m2:
                            continue
                        if check(n, f'{a1}-{b1},{a2}-{b2}',
                                 f'{n1}/{m1},{n2}/{m2},{n - b2}', fails):
                            a3_fail += 1
    for (n, a1, b1, a2, b2, a3, b3) in [
        (10, 4, 4, 6, 6, 8, 8),
        (11, 4, 4, 6, 7, 9, 9),
        (12, 4, 4, 7, 8, 10, 10),
        (13, 5, 5, 7, 8, 10, 11),
        (13, 6, 6, 8, 10, 12, 12),
        (16, 5, 6, 8, 10, 12, 13),
        (20, 7, 8, 11, 13, 16, 17),
    ]:
        n1, m1 = a1 - 1, b1 - a1 + 1
        n2, m2 = a2 - b1 - 1, b2 - a2 + 1
        n3, m3 = a3 - b2 - 1, b3 - a3 + 1
        if min(n1 - m1, n2 - m2, n3 - m3) < 0 or a3 >= n:
            continue
        check(n, f'{a1}-{b1},{a2}-{b2},{a3}-{b3}',
              f'{n1}/{m1},{n2}/{m2},{n3}/{m3},{n - b3}', fails)
    # p=4 の代表配置
    for (n, segs, spec) in [
        (13, '3/1,1/1,1/1,2', '4-4,6-6,8-8'),
        (13, '2/1,1/1,1/1,1,2', '3-3,5-5,7-7,9-9'),
    ]:
        check(n, spec, segs, fails)
    print(f'A2 sweep: {fails[1]} layouts, {fails[0]} failures (A3-FAIL 内含 {a3_fail}, 発火は区別なし)')


# ── A2/A3 の区間一般での検証(スケジュール式に対応した期待配置) ──────────
# sizes = [n1, m1, n2, m2, ..., n_{p+1}] (I と D の交互)。a3 フラグで
#   A2 (Thm.3): t_i = 2Σ_{j<i}(n_j+m_j)、Δt_i = 2m_i+2Σ_{j>i}(n_j+m_j)+2n_{p+1}
#   A3 (Thm.4): t_i = 2Σ_{j<i}(...)+(i-1)、Δt_i = 上式 + (p-i+1)
# 各 I_i は t_{i1} = t_i + n_i - 1 から Δt_i 行凍結。I_{p+1} は t_i 起動・無凍結。
def seg_mode_cases():
    return [
        (a3, n, spec, sizes)
        for (a3, n, spec, sizes) in [
            (True, 10, '4,7', [3, 1, 2, 1, 3]),
            (False, 10, '4,7', [3, 1, 2, 1, 3]),
            (True, 13, '5-6,9-9', [4, 2, 2, 1, 4]),
            (True, 13, '5-6,9-10', [4, 2, 2, 2, 3]),
            (True, 16, '5-6,9-10,13-13', [4, 2, 2, 2, 2, 1, 3]),
            (True, 20, '7-8,11-13', [6, 2, 2, 3, 7]),
            (True, 13, '3,7,11', [2, 1, 3, 1, 3, 1, 2]),
        ]
    ]


def seg_expected(a3, sizes, golds):
    n = sum(sizes)
    p = (len(sizes) - 1) // 2

    def t_i(i):
        s = 2 * sum(sizes[0:2 * (i - 1)])
        if a3:
            s += i - 1
        return s

    def delta(i):
        d = 2 * sizes[2 * i - 1] + 2 * sum(sizes[2 * i:2 * p]) + 2 * sizes[-1]
        if a3:
            d += p - i + 1
        return d

    # セル番号 → (区間種, 区間番号, 区間内座標)
    pos = []
    x = 1
    for k, sz in enumerate(sizes):
        intact = k % 2 == 0
        idx = k // 2 + 1
        for _ in range(sz):
            if intact:
                pos.append(('I', idx, x))
            else:
                pos.append(('D', idx, x))
            x += 1

    rows = []
    fire_t = 2 * n - 2 + (p if a3 else 0)
    for t in range(0, fire_t + 1):
        row = []
        for (kind, idx, seg_x) in pos:
            if kind == 'D':
                row.append('L')
            else:
                seg_start = sum(sizes[0:2 * (idx - 1)]) + 1
                j = seg_x - seg_start + 1  # 区間内座標 (1 起点)
                n_seg = sizes[2 * idx - 2]
                t_st = t_i(idx)
                g = golds[idx - 1]
                if idx <= p:
                    # f = t_i1 + (n_i - j): セル j の凍結表示開始時刻
                    f_ = t_st + n_seg - 1 + (n_seg - j)
                    dd = delta(idx)
                    if t < t_st:
                        row.append('L')
                    elif t < f_:
                        row.append(g[t - t_st][j - 1])
                    elif t < f_ + dd:
                        if j == n_seg and t == f_ and n_seg >= 2:
                            row.append('CB')  # 境界の1ステップCarrier
                        else:
                            held = ('L' if f_ - 1 - t_st < 0
                                    else g[f_ - 1 - t_st][j - 1])
                            row.append('F' + held)
                    else:
                        row.append(g[t - dd - t_st][j - 1])
                else:
                    if t < t_st:
                        row.append('L')
                    else:
                        row.append(g[t - t_st][j - 1])
        rows.append(row)
    return rows


def verify_seg(a3_only=False):
    ok = True
    for (a3, n, spec, sizes) in seg_mode_cases():
        if a3_only and not a3:
            continue
        p = (len(sizes) - 1) // 2
        golds = [[cells(r) for r in golden_rows(sz)]
                 for idx_i, sz in [(k, sizes[k]) for k in range(0, len(sizes), 2)]]
        flag = ['--a3'] if a3 else []
        act = state_rows(run(BIN, ['-r', OUT_RULE, '-s', str(n), '-d', '-f', spec] + flag))
        exp = seg_expected(a3, sizes, golds)
        bad = 0
        for t, row in enumerate(act):
            if t >= len(exp):
                bad += 1
                continue
            ac, ec = cells(row), exp[t]
            for a, e in zip(ac, ec):
                if a.strip() != e.strip():
                    bad += 1
        want = 2 * n - 2 + (p if a3 else 0) + 1
        fire_ok = len(act) == want
        status = 'OK' if (bad == 0 and fire_ok) else 'NG'
        if status == 'NG':
            ok = False
        mode = 'A3' if a3 else 'A2'
        print(f'{mode} n={n} f={spec}: {status} (mismatched={bad}, '
              f'fire={len(act) - 1} want={want - 1})')
    print('ALL OK' if ok else 'FAILURES PRESENT')


def sweep_a3():
    # A3 条件 (n_i >= m_i かつ n_i + m_i >= p - i) を満たす p>=1 配置の列挙。
    # 期待 = SYNCED at 2n-2+p。
    import itertools
    total = fails = 0
    for n in range(5, 15):
        for a1 in range(2, n - 1):
            for b1 in range(a1, n - 1):
                for a2 in range(b1 + 2, n):
                    for b2 in range(a2, n):
                        if b2 >= n:
                            continue
                        segs = [(1, a1 - 1), (a1, b1), (b1 + 1, a2 - 1), (a2, b2), (b2 + 1, n)]
                        sizes = [s[1] - s[0] + 1 for s in segs]
                        if sizes[0] < 1 or sizes[-1] < 1:
                            continue
                        ok_c = True
                        p = 2
                        for j in (1, 2):  # D_j は sizes の 2j、その左の I は 2j-2
                            n_j, m_j = sizes[2 * j - 2], sizes[2 * j - 1]
                            # A3 条件: n_i >= m_i かつ n_i + m_i >= p - i
                            if n_j < m_j or n_j + m_j < p - j:
                                ok_c = False
                        if not ok_c:
                            continue
                        total += 1
                        out = run(BIN, [
                            '-r', OUT_RULE, '-s', str(n),
                            '-f', f'{a1}-{b1},{a2}-{b2}', '--a3'])
                        if f'SYNCED (intact cells) at t={2 * n - 2 + 2}' not in out:
                            fails += 1
                            print(f'FAIL n={n} -f {a1}-{b1},{a2}-{b2} (sizes={sizes})')
    # p=3: 全配置列挙 (n=8..14, n_i >= m_i で A3 条件は p<=3 では自明)。
    for n in range(8, 15):
        for a1 in range(2, n - 3):
            for b1 in range(a1, n - 3):
                for a2 in range(b1 + 2, n - 1):
                    for b2 in range(a2, n - 1):
                        for a3b in range(b2 + 2, n):
                            for b3 in range(a3b, n):
                                if b3 >= n:
                                    continue
                                segs = [(1, a1 - 1), (a1, b1),
                                        (b1 + 1, a2 - 1), (a2, b2),
                                        (b2 + 1, a3b - 1), (a3b, b3),
                                        (b3 + 1, n)]
                                sizes = [s[1] - s[0] + 1 for s in segs]
                                if sizes[0] < 1 or sizes[-1] < 1:
                                    continue
                                if any(sizes[2 * j - 2] < sizes[2 * j - 1]
                                       for j in (1, 2, 3)):
                                    continue
                                total += 1
                                spec3 = (f'{a1}-{b1},{a2}-{b2},'
                                         + (f'{a3b}' if a3b == b3 else f'{a3b}-{b3}'))
                                out = run(BIN, [
                                    '-r', OUT_RULE, '-s', str(n),
                                    '-f', spec3, '--a3'])
                                if f'SYNCED (intact cells) at t={2 * n - 2 + 3}' not in out:
                                    fails += 1
                                    print(f'FAIL p3 n={n} -f {spec3} (sizes={sizes})')
    print(f'A3 sweep: {total} layouts, {fails} failures (p=2 + p=3)')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'verify'
    {'gen': gen, 'discover': discover, 'verify': verify, 'add_a1': add_a1,
     'verify_a1': verify_a1, 'discover_a1': discover_a1,
     'sweep_a2': sweep_a2, 'verify_seg': verify_seg,
     'sweep_a3': sweep_a3}[cmd]()