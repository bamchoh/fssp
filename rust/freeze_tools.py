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

# -I 実行でも同居モジュール(check_keys など)を import できるようにする
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
    # TR: fast 継続信号の対岸横断状態。CB から管路 to_right に入った fast 信号を
    # 未起床セル上で 1/1 で運ぶ(起床前のセルだけが保持できる=起床・choreography
    # の前を必ず横断する)。壁で (TR,L,www) -> FT と 0 成本で折り返す。
    if 'TR' not in names:
        states.append(f'{pad("TR")}@101010,00CCAA,others')
        names.append('TR')
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
        # fast 継続信号の対岸横断(規則層):
        #   - 入口 pickup 経由で CB が対岸の左入力に現れると横断状態 TR に
        #     (未起床セルのみ。L と www の相手を横断する)
        #   - 配列右端(www と隣接)で 0 成本で解凍信号 FT に折り返す
        #   - FT は左走行で未起床セルを戻り、管路遠位口 pickup で to_left に
        #     乗せられて R_i の右入力に提示される(時刻式は往復 1/1 から自動一致)
        #   - 波は 1 セル 1 ステップの提示で進み、すぐもとのアイドルに戻る
        (('CB', 'L', 'L'), 'TR'),
        (('CB', 'L', 'www'), 'FT'),  # 対岸が 1 セル区間: 受容と同時に折り返し
        (('TR', 'L', 'L'), 'TR'),
        (('TR', 'L', 'www'), 'FT'),
        (('TR', 'L', 'W'), 'TR'),  # p>=2: 通過のまま次の管路の入口 pickup へ
        # TR 波の通過セルの元のアイドル提示に戻る(左隣は管路 www か アイドル L、
        # 右隣はアイドル L / TR 波 / 折り返し後の FT が見える)
        (('L', 'TR', 'L'), 'L'),
        (('www', 'TR', 'L'), 'L'),
        (('L', 'TR', 'FT'), 'L'),
        (('www', 'TR', 'FT'), 'L'),
        # 折り返し FT の左走行波(未起床セル上)
        (('www', 'L', 'FT'), 'FT'),
        (('L', 'L', 'FT'), 'FT'),
        (('L', 'FT', 'L'), 'L'),
        (('www', 'FT', 'L'), 'L'),
        (('L', 'FT', 'www'), 'L'),
        # TR 波が右隣で通行しているセル(アイドル)は変化しない
        (('www', 'L', 'TR'), 'L'),
        (('L', 'L', 'TR'), 'L'),
        # FT 波が左隣(すでに通行済み)にあるアイドルセルも変化しない
        (('FT', 'L', 'www'), 'L'),
        (('FT', 'L', 'L'), 'L'),
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
    # これを F(L)=FL と同じに扱う。(x, FL, r) -> y を (x, CB, r) に、
    # (l, c, FL) -> y を (l, c, CB) に複製(CB は FL と同じ機械として振る舞い、
    # 加えて fast 信号の管路引き渡しを担う)。
    cb_copies = 0
    for key in list(rulemap.keys()):
        for slot, old_val in [(1, 'FL'), (2, 'FL')]:
            if key[slot] != old_val:
                continue
            nk = list(key)
            nk[slot] = 'CB'
            new_key = tuple(nk)
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
    # 会合機械入りルールの障害なし走行(凍結ケースの基準。機械自身の
    # S33 排出表示も期待どおりに含むことに注意)
    return state_rows(run(BIN, ['-r', OUT_RULE, '-s', str(n), '-d']))


def golden_base(n):
    # 素の balzer の障害なし走行(A1 の基準 choreography。会合機械は
    # 凍結機械と同居するため、I1 の基準には素のルールセットを使う)
    return state_rows(run(BIN, ['-r', BASE_RULE, '-s', str(n), '-d']))


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
    (4, 3, 23),   # n=30 -f 5-7 (I2 が長い=壁反射波が起床済み区間を横断する)
]


def a1_expected(gold1_c, gold2_c, n1, m1, n2):
    n = n1 + m1 + n2
    t1 = n1 - 1
    dt = 2 * m1 + 2 * n2
    T2 = 2 * (n1 + m1)
    S = n1 + m1
    # 会合セルと NG 出発時刻(会合メカニズム最終形、T0=1 表示時刻系)。
    #   偶数 S: c = S/2+1、会合キー (R2 が右隣) → NG 表示 t = 3S/2
    #   奇数 S: c = (S+3)/2、会合キー (R2 が中央) → NG 表示 t = (3S+1)/2
    # c = S−1+c = t_dep が M@2S の判定式と一致する唯一の会合セル。
    c_m = S // 2 + 1 if S % 2 == 0 else (S + 3) // 2
    t_dep = S - 1 + c_m
    # 会合が I1 に収まらない配置(m1 >= n1−1)では従来模型のまま(注入代替)
    meet_ok = c_m <= n1
    ph = ['S33', 'S3A', 'S3B']
    rows = []
    for t in range(0, 2 * n - 2 + 1):
        row = []
        for x in range(1, n + 1):
            if x <= n1:
                f_ = t1 + (n1 - x)
                v_x = gold1_c[f_ - 1][x - 1].strip() if f_ >= 1 else 'L'
                d = None
                if t < f_:
                    d = gold1_c[t][x - 1]
                elif t < f_ + dt:
                    if x == n1 and t == f_ and n1 >= 2:
                        d = 'CB'  # 境界の1ステップCarrier
                    else:
                        d = 'F' + v_x
                else:
                    d = gold1_c[t - dt][x - 1]
                # S33 journey(tau(x) = 3x−2 に 3 歩滞在)。会合が成立する配置では
                # x <= c−1、不成立帯(c > n1)では journey 全長 x <= n1。
                # 内部複合 = 小文字(choreography 仮想値を運ぶ)、
                # 氷結複合 = 大文字(F_v 一定)。off = f−tau = 2n1+1−4x ≡ 奇数で
                # 交差は off ∈ {1, 3} のみ(off 0/2 は構造的に起こらない)。
                j_max = c_m - 1 if meet_ok else n1
                if meet_ok and c_m <= x <= n1 and t < f_ + dt:
                    # NG 会合放出(1/1 右走行)。会合セル(奇数 S)は journey の
                    # 再点火を防ぐため 2 歩表示、それ以降(c より右)は 1 歩
                    if x == c_m and S % 2 == 1:
                        if t in (t_dep, t_dep + 1):
                            d = f'{v_x}NG'
                    elif t == t_dep + (x - c_m):
                        d = f'{v_x}NG'
                if x <= j_max:
                        tau = 3 * x - 2
                        off = f_ - tau
                        if off <= -1:
                            # 到来前に凍結済み: 保値付き位相 carrier
                            if tau <= t <= tau + 2:
                                d = f'{v_x}{ph[t - tau]}'
                        elif off >= 4:
                            # 滞在完了後に前線が届く: 内部複合で choreography を
                            # 仮想計算し続ける(表示は.golden 値の複合)
                            if tau <= t <= tau + 2:
                                d = gold1_c[t][x - 1].strip().lower() + ph[t - tau]
                        elif off == 1:
                            # 発火表示の翌ステップで氷結複合へ(held = gold[f−1])
                            if t == tau:
                                d = gold1_c[tau][x - 1].strip().lower() + 'S33'
                            elif t == tau + 1:
                                d = gold1_c[tau][x - 1].strip() + 'S3A'
                            elif t == tau + 2:
                                d = gold1_c[tau][x - 1].strip() + 'S3B'
                            # t >= tau+3 は標準 F 表示('F'+v_x)に合流
                        else:  # off == 3
                            # 滞在 3 歩完了 → ph2 が x+1 の触発と氷結復帰を兼ねる
                            # (t = tau+3 = f_ は標準 'F'+v_x と一致)
                            if tau <= t <= tau + 2:
                                d = gold1_c[t][x - 1].strip().lower() + ph[t - tau]
                # R2(1/1 左走行、表示は 1 歩)。会合は吸収するので(奇数 S では
                # 会合セル自身、偶数 S ではその右隣まで)。会合不成立帯では R2 は
                # I1 末端(=会合セル候補)の journey 保値復帰の瞬間に重なって吸収
                # され、以後 carrier が発生しないのでどこにも表示されない。
                # 滞在中に重なる 4x ∈ {2S, 2S+2} のセルでは carrier 表示が優先
                # (R2 は表示されずに消える)
                if meet_ok and x >= 1 and t < f_ + dt and t == 2 * S + 1 - x:
                    r2_lo = c_m if S % 2 == 1 else c_m + 1
                    if x >= r2_lo and d not in (
                            None, 'CB') and not d.endswith(('S33', 'S3A', 'S3B')):
                        d = f'{v_x}R2'
                row.append(d)
            elif x <= n1 + m1:
                row.append('L')
            else:
                x2 = x - (n1 + m1)
                if n1 >= 2:
                    # fast 継続信号の横断: TR 波は未起床セルを 1/1 で右へ
                    # (CB@t_i1 → to_right pickup → 対岸提示 t_i1+m1+1 から)、
                    # 折り返し後の波は左へ 1/1 で戻る。通行の提示はセルがまだ
                    # アイドルの間だけ(起床後のシャトーには重ねられない=複合
                    # carrier の増分は後段)。壁セル(x2 = n2)では通行と同等に
                    # 折り返し FT だけを見える。
                    fb = next((k for k, gr in enumerate(gold2_c)
                               if gr[x2 - 1].strip() != 'L'), None)
                    fb = fb if fb is not None else 10 ** 9
                    t_tr = t1 + m1 + 1 + (x2 - 1)
                    t_ft = t1 + m1 + n2 + (n2 - x2)
                    if t == t_ft:
                        # 壁反射の左走行波 T3: 未起床セルでは純 T3、起床済みセル
                        # では複合 carrier <X>T3(choreography を重畳保持する)
                        if t < T2 + fb:
                            row.append('T3')
                        else:
                            row.append(gold2_c[t - T2][x2 - 1].strip() + 'T3')
                    elif t == t_tr and t_tr <= T2 + fb and x2 < n2:
                        row.append('TR')
                    elif t >= T2:
                        row.append(gold2_c[t - T2][x2 - 1])
                    else:
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


def add_composite():
    # CA 局所化 第 2 段(実装): 壁反射の左走行解凍波 T3 と、起床済みセル上の
    # 複合 carrier <X>T3(chateau+signal)。論文の第 3 反射信号は I_{p+1} を左走行
    # して I_i の右端で解凍を起動する(p=1 では to_left 管路に回収されて I1 を
    # 解凍)。横断先のセルが起床済み(choreography 表示中)のとき、波の通過で
    # choreography を止めることはできない(I_{p+1} は 2n-2 で無遅延発火する)ので:
    #   - 吸収:  (l, X, <Y>T3) -> <f(l,X,Y)>T3。セルは複合表示になり、自身の
    #            choreography の次状態 f(l,X,Y) を複合の X 部分に保持する。波は
    #            1/1 で進むので時刻に遅延は生じない。
    #   - 放出:  (l, <X>X>T3, r) ではなく (l, <X>T3, r) -> f(l,X,r)。次ステップ
    #            で choreography に戻り、波は左隣へ(左隣は次の吸収を行う)。
    # 未起床(静止 L)セル上では波は純 T3 のまま走る。管路の遠位口 pickup は
    # (rust 側)T3 / 複合を検知すると純 FT に変換して管路に乗せる。
    states, rules, pre, post = load(OUT_RULE)
    rulemap = parse_rules(rules)
    names = [state_name(s) for s in states]
    interior = ['A', 'B', 'C', 'L', 'M', 'Q', 'R']
    composites = [s + 'T3' for s in interior]
    if 'T3' not in names:
        states.append(pad('T3') + '@101010,D08040,others')
        names.append('T3')
    for c in composites:
        if c not in names:
            states.append(pad(c) + '@101010,B070C0,others')
            names.append(c)
    # 1. 壁反射の折り返し(wip の FT 直接流用を T3 に。I_{p+1}=1 セル区間の
    #    即時折り返しと、FT2 位相の管路がある場合の折り返しも T3 へ)
    rulemap[('TR', 'L', 'www')] = 'T3'
    rulemap[('CB', 'L', 'www')] = 'T3'
    rulemap[('TR', 'L', 'FT2')] = 'T3'
    rulemap[('CB', 'L', 'FT2')] = 'T3'
    # 1b. p >= 2: 横断の入口・放出の管路境界(W 提示)コンテキスト。I_{i+1} が
    #     1 セルでその右隣が次の管路の場合、入口/放出の右入力は W。
    rulemap[('CB', 'L', 'W')] = 'TR'
    for x in ['L', 'www']:
        rulemap[(x, 'TR', 'W')] = 'L'
    # 1c. 出入口の衝突: 左入力に CB(区間の送出)が流れていて、右隣に左走行波
    #     (純 T3 / 複合 <Y>T3)が同時にいる場合、表示は横断 TR を優先する
    #     (送出波の旅程が優先。T3 は次ステップで管路の口で消える)。
    for y in ['T3'] + [s + 'T3' for s in ['A', 'B', 'C', 'L', 'M', 'Q', 'R']]:
        rulemap[('CB', 'L', y)] = 'TR'
    # 2. 未起床セル上の左走行波(wip の FT 直接流用を T3 に置換し、FT の
    #    未起床セル上の振る舞いは base の規則に復帰する)
    rulemap[('L', 'L', 'FT')] = 'L'
    rulemap[('www', 'L', 'FT')] = 'L'
    for (l, c, r) in [
        ('L', 'FT', 'L'), ('www', 'FT', 'L'), ('L', 'FT', 'www'),
    ]:
        rulemap[(l, c, r)] = 'L'
    for (l, c, r) in [
        ('www', 'L', 'T3'), ('L', 'L', 'T3'),
        ('L', 'T3', 'L'), ('www', 'T3', 'L'), ('L', 'T3', 'www'),
    ]:
        rulemap[(l, c, r)] = 'T3' if c == 'L' else 'L'
    # 境界の左右で状態が違うケース: 凍結・起床済みの左隣を持つ静止ホスト
    # (l = choreography、r = L)の放出も L。
    for x in ['www', 'L'] + interior:
        rulemap[(x, 'T3', 'L')] = 'L'
        rulemap[(x, 'T3', 'www')] = 'L'
    # ホストの放出とウェーブ起動の同時機(通過の 1 ステップ後に起床が来る):
    # 左隣が起動前兆(C/M/NG)のときは放出先が起動先表示になる。
    # (未起床セルが f(l,L,r) で C/M に起動する規則 (C,L,L)->C、(M,L,L)->C、
    #  (NG,L,L)->M の機械的続き)
    rulemap[('C', 'T3', 'L')] = 'C'
    rulemap[('M', 'T3', 'L')] = 'C'
    rulemap[('NG', 'T3', 'L')] = 'M'
    # 通過済みの隣接セル(波が 1 セル移動した後の右側のセル): 左に入った
    # T3 に触れても変化しない。
    rulemap[('T3', 'L', 'L')] = 'L'
    rulemap[('T3', 'L', 'www')] = 'L'
    # 3. 放出 (center 偽装): 起床済み表示 X 上の複合は X と同じ遷移をする
    n_release = 0
    for key in list(rulemap.keys()):
        (l, c, r) = key
        if c in interior:
            nk = (l, c + 'T3', r)
            if rulemap.get(nk) != rulemap[key]:
                rulemap[nk] = rulemap[key]
                n_release += 1
    # 3b. 通過済み隣接の左入力偽装(左隣が複合のセル): 左隣の choreography
    # 値 X で置き換えた同じ遷移をする(起床波の伝播も複合越しに続く。純 T3
    # が左にある場合は 2 の放出規則が L を返すので別扱い)
    n_left = 0
    for key in list(rulemap.keys()):
        (l, c, r) = key
        if l in interior:
            nk = (l + 'T3', c, r)
            if rulemap.get(nk) != rulemap[key]:
                rulemap[nk] = rulemap[key]
                n_left += 1
    # 4. 吸収: (l, X, <Y>T3) -> <f(l,X,Y)>T3 / (l, X, T3) -> <f(l,X,L)>T3
    #    (target が choreography 表示になる組のみ。吸収は複合の Y 部分から
    #     右隣の choreography 値を取り出して通常遷移と同じ次状態を保持する)
    n_absorb = 0
    for key in list(rulemap.keys()):
        (l, c, r) = key
        y = rulemap[key]
        if c in interior and c != 'L' and r in interior and y in interior:
            nk = (l, c, r + 'T3')
            if rulemap.get(nk) != y + 'T3':
                rulemap[nk] = y + 'T3'
                n_absorb += 1
        if c in interior and c != 'L' and r == 'L' and y in interior:
            nk = (l, c, 'T3')
            if rulemap.get(nk) != y + 'T3':
                rulemap[nk] = y + 'T3'
                n_absorb += 1
    # 2b. 接触(波の通過とウェーブ起動が同時刻)の吸収: セルが起床する瞬間に
    #     波が通りかかる場合は複合で両方を担う(左隣の C/M/NG = ウェーブ起動
    #     の前兆表示で区別。f(l,L,L) = 起動先表示と同じになる)。
    rulemap[('C', 'L', 'T3')] = 'CT3'
    rulemap[('M', 'L', 'T3')] = 'CT3'
    rulemap[('NG', 'L', 'T3')] = 'MT3'
    # 5. 複合の左隣が静止 L の場合も純 T3 で貫通(p >= 2 の未起床帯を予約。
    #    上の吸収生成が (l, 'L', <Y>T3) を作った場合はここで上書きする)
    n_asleep = 0
    for y in interior:
        yk = y + 'T3'
        for l in ['www', 'L'] + interior:
            if rulemap.get((l, 'L', yk)) != 'T3':
                rulemap[(l, 'L', yk)] = 'T3'
                n_asleep += 1
    ordered = emit(rulemap, list(rulemap.keys()))
    write_file(states, pre, post, ordered)
    print(f'add_composite: states={len(states)} rules={len(ordered)} '
          f'(+{n_release} release +{n_absorb} absorb +{n_asleep} asleep)')


def add_meeting():
    # 会合メカニズム(MEMO「会合メカニズム最終形」実装、2026-10-16 解析+実装検算)。
    #   S33(1/3 信号): 論文の 1/3 放出の CA 局所化=細胞 1 の将軍が
    #     (www, M, L) -> 複合(仮想値 M+S33)で t=1 に放出(T0=1、表示時刻系
    #     tau(x) = 3x-2、滞在 3 歩 S33->S3A->S3B、各 1 歩表示)。
    #   複合 carrier は 2 種:
    #     内部複合 lo[X]+ph(小文字): choreography 表示 X+信号。位相進行のたびに
    #       仮想値を f(l,X,r)(現行 rulemap をオラクルに使用)で計算し続けるので、
    #       滞在中も choreography が正確に進み、復帰 (l, 複合S3B, r) -> f(l,X,r)
    #       でシームレスに合流する(氷結前線との干渉なし)。
    #     氷結複合 X+ph(大文字 = F_v+信号): 凍結セル上は仮想値一定 v。
    #   R2(反射信号の I1 極性版): to_left 遠位口の TR 装填(rust 側)から 1/1 左走行、
    #     表示 rho(x) = 2S+1-x。会合は時間一致が 4x=2S+2(S 偶数: R2 右隣キー,
    #     c = S/2+1)/4x=2S+3(S 奇数: R2 中央キー, c=(S+3)/2)で排他的に一致する。
    #   NG 会合放出: 会合セルで 1 歩表示(奇数 S は 2 歩: 左隣 ph2 触発の再点火防止)
    #     し、1/1 右走行して I1 右端から to_right 入口 pickup(rust 側)で管路へ。
    # 交差(off = f-tau = 2n1+1-4x ≡ 奇数)は off ∈ {1, 3} のみ(off 0/2 は不可能)。
    #   off=1: 発火表示の次ステップで氷結複合へ(off>=1 なので発火は完了)。
    #   off=3: 滞在 3 歩完了後、ph2 表示が x+1 の触発と held=gold[f-1] への復帰を
    #     同時に担う(= 標準 F 表示にそのまま合流)。
    # 追加状態 = 内部複合 21 + 氷結複合 21 + R2 7 + NG 7 = 56 → 計 87 状態。
    states, rules, pre, post = load(OUT_RULE)
    rulemap = parse_rules(rules)
    names = [state_name(s) for s in states]
    held = ['A', 'B', 'C', 'L', 'M', 'Q', 'R']   # 保値/choreography 7
    fv = ['F' + s for s in held]
    lo = {h: h.lower() for h in held}
    phn = ('S33', 'S3A', 'S3B')
    color = {'R2': 'E07820', 'NG': '50C050'}
    new = ([lo[h] + ph for h in held for ph in phn]
           + [h + ph for h in held for ph in phn]
           + [h + 'R2' for h in held] + [h + 'NG' for h in held])
    for nm in new:
        if nm not in names:
            c = color.get(nm[-3:], 'B070C0')
            if nm.endswith('S33') or nm.endswith('S3A') or nm.endswith('S3B'):
                c = 'D8C840' if nm[0].islower() else '40A8C8'
            states.append(pad(nm) + f'@101010,{c},others')
            names.append(nm)
    n_override = [0]

    def put(key, target):
        if target is None:
            return  # オラクル未定義キー: discover_a1 の合成に任せる
        if rulemap.get(key) != target:
            if key in rulemap:
                n_override[0] += 1
            rulemap[key] = target

    def F(l, X, r):
        # 現行ルール表を choreography 遷移のオラクルとして使う
        # (add_a1/discover で合成済みの (l,X,r) キー、境界の W/www 提示を含む)。
        # choreography 内部状態以外の返値(CB などの一過性 carrier)は複合の
        # 仮想値にならないので None 扱い(discover_a1 の合成に任せる)
        t = rulemap.get((l, X, r))
        return t if t in held else None

    # -- S33 誕生(細胞 1 の将軍、t=1 表示)= 論文の 1/3 放出 -------------------
    cur0 = rulemap.get(('www', 'M', 'L'))
    if isinstance(cur0, str) and cur0[:1].islower():
        put(('www', 'M', 'L'), cur0)  # 再実行: 既に排出複合のまま
    else:
        L0 = (F('www', 'M', 'L') or 'M').strip()  # 理論値: 細胞1の golden 表示
        put(('www', 'M', 'L'), lo[L0] + 'S33')
    # 細胞1は発射後も M 表示を継続(golden)。右隣は S33/R2/NG の複合表示
    for r in ([lo[h] + ph for h in held for ph in phn]
              + [h + ph for h in held for ph in phn]):
        put(('www', 'M', r), 'M')
    # -- 発火(左隣の ph2 複合が自身の仮想値を運ぶ)----------------------------
    # 内部受信: 仮想値を 1 歩進めて複合を開始
    for X in held:
        for Y in held:
            for r in held + ['W']:
                t0 = F(X, Y, r)
                if t0 is None:
                    continue  # 未定義キーは discover_a1 が合成する
                put((lo[X] + 'S3B', Y, r), lo[t0] + 'S33')
        # 氷結受信(交差境界 y)と氷結領域内の伝播(保値 v のまま複合開始)
        for v in held:
            for r in fv + ['W', 'CB', 'TR']:
                put((lo[X] + 'S3B', 'F' + v, r), v + 'S33')
                put((X + 'S3B', 'F' + v, r), v + 'S33')
    # -- 内部複合の位相進行と復帰(仮想値を choreography どおり進める)---------
    for X in held:
        for l in ['www'] + held:
            for r in held:
                t1 = F(l, X, r)
                if t1 is None:
                    continue  # 未定義キーは discover_a1 が合成する
                put((l, lo[X] + 'S33', r), lo[t1] + 'S3A')
                put((l, lo[X] + 'S3A', r), lo[t1] + 'S3B')
                put((l, lo[X] + 'S3B', r), t1)  # 復帰 = choreography の次状態
    # -- 交差(右隣が凍結表示 = 前線が滞在中に到達): 氷結複合へ切替 ------------
    # held = 発火時の仮想値(= gold[f-1])なので復帰後の F 表示とも一致する
    for X in held:
        for l in ['www'] + held:
            for r in fv + ['CB']:
                put((l, lo[X] + 'S33', r), X + 'S3A')
                put((l, lo[X] + 'S3A', r), X + 'S3B')
                put((l, lo[X] + 'S3B', r), 'F' + X)
    # -- 氷結複合の進行(保値一定)と復帰 --------------------------------------
    for X in held:
        for l in ['www'] + held + fv:
            for r in fv + held + ['W', 'CB', 'TR']:
                put((l, X + 'S33', r), X + 'S3A')
                put((l, X + 'S3A', r), X + 'S3B')
                put((l, X + 'S3B', r), 'F' + X)
    # -- 待機ホスト(左隣が ph0/ph1 を表示中): choreography は仮想値で計算 ----
    # 内部ホスト: 自身の表示 Y を 1 歩進める(左隣の仮想値 X を左入力に使う)
    for X in held:
        for Y in held:
            for r in held + ['W']:
                for ph in ('S33', 'S3A'):
                    put((lo[X] + ph, Y, r), F(X, Y, r))  # None は put で握り潰す
        # 氷結ホスト / 氷結領域の保持(複合の左隣は自分に影響しない)。
        # ph2 は右隣の触発なので保持に入れない(発火族との競合を避ける)
        for ph in ('S33', 'S3A'):
            for v in held:
                for r in fv + ['W', 'CB', 'TR']:
                    put((lo[X] + ph, 'F' + v, r), 'F' + v)
                    put((X + ph, 'F' + v, r), 'F' + v)
    # -- 波の右側の隣接セル(左隣が複合を表示中): choreography は左隣の仮想値
    #    を右入力に使って進む(複合 = choreography+signal の透過性)--------------
    fheld = ['www'] + fv
    lowall = [lo[h] + ph for h in held for ph in phn]
    upall = [h + ph for h in held for ph in phn]
    for X in held:
        for l in ['www'] + held:
            for c in held:
                for ph in phn:
                    put((l, c, lo[X] + ph), F(l, c, X))
                    put((l, c, X + ph), F(l, c, X))  # 右隣が氷結複合のときも同様
# (会合直前/直後は r が NG/R2 複合になる)
    # r = NG 複合(会合セル直前の復帰: 自己仮想値 X を中央に置き w を右隣に見る)
    for X in held:
        for l in ['www'] + held + fv + [h + 'NG' for h in held]:
            for w in held:
                put((l, lo[X] + 'S3B', w + 'NG'), F(l, X, w))
                put((l, X + 'S3B', w + 'NG'), 'F' + X)  # 氷結複合側の復帰(会合右)
# 会合直後: 位相進行の r-lane も NG/R2 複合(会合セル直前 c−1 の位相進行は
# NG/R2 表示と同時に起きる。l は凍結複合も含む=氷結stretchの左隣が凍結表示)
    for X in held:
        for l in ['www'] + held + fv:
            for w in held:
                for ph in ('S33', 'S3A'):
                    put((l, lo[X] + ph, w + 'NG'), lo[F(l, X, w) or X] + (phn[1] if ph == phn[0] else phn[2]))
                    put((l, X + ph, w + 'NG'), X + (phn[1] if ph == phn[0] else phn[2]))
                    # R2 複合が右隣に来ても滞在は予定どおり進む(R2 は消える:
                    # 会合で吸収/会合不成立帯では以後無意味)。R2 の 1/1 表示が
                    # 滞在中に重なるのは 4x ∈ {2S, 2S+2} の c−1 だけ
                    put((l, lo[X] + ph, w + 'R2'), lo[F(l, X, w) or X] + (phn[1] if ph == phn[0] else phn[2]))
                    put((l, X + ph, w + 'R2'), X + (phn[1] if ph == phn[0] else phn[2]))
    # r = 複合(内部/氷結)を左隣に見る凍結ホストの保持(ph は右隣レーンとして
    # 全位相を許す。fire 族は複合を左隣に取る形なので競合しない))
    for X in held:
        for l in (['www'] + held + fv
                  + [lo[h2] + ph for h2 in held for ph in ('S33', 'S3A', 'S3B')]
                  + [h2 + ph for h2 in held for ph in ('S33', 'S3A', 'S3B')]):
            for v in held:
                for ph in ('S33', 'S3A', 'S3B'):
                    put((l, 'F' + v, X + ph), 'F' + v)
                    put((l, 'F' + v, lo[X] + ph), 'F' + v)
            for v in held:
                for w in held:
                    put((l, 'F' + v, w + 'NG'), 'F' + v)   # NG の左隣の保持
                    put((l, 'F' + v, w + 'R2'), 'F' + v)   # R2 の左隣の保持

    # -- 標準の前線/ホールド族に複合を左隣に見るレーンを足す(journey 中のセルの
    #    左隣が複合のときも前線は予定時刻どおり点火する)
    for l in [lo[h] + ph for h in held for ph in phn] +              [h + ph for h in held for ph in phn]:
        for c in held:
            for r in fv + ['FS', 'CB']:
                put((l, c, r), 'F' + c)

    # -- 前線は氷結複合表示を透過する(off=1 交差セルの表示が右端になった直後、
    #    左隣の前線が予定時刻どおり点火できるように: (l, c, 氷結複合ph1/2) -> F(c))
    for X in held:
        for l in ['www', 'L', 'M'] + held + fv +                  [lo[h] + ph for h in held for ph in phn] +                  [h + ph for h in held for ph in phn]:
            for c in held:
                put((l, c, X + 'S3A'), 'F' + c)
                put((l, c, X + 'S3B'), 'F' + c)

    # -- 管路の L→R アイドルは L を提示する(rust 側)。I_{i+1} 左端の
    #    (www, M, r) 境界規則を (L, M, r) にミラーする(ただし S33 排出を
    #    除く: (www, M, L) のみ排出、(L, M, r) はすべて保持)
    for r in (['L', 'www'] + held + fv
              + [lo[h] + ph for h in held for ph in phn]
              + [h + ph for h in held for ph in phn]
              + ['T3'] + [h + 'T3' for h in held]):
        t0 = rulemap.get(('www', 'M', r))
        # S33 排出(composite を返す (www, M, L))以外をミラーする。排出は保持
        # (M)に置き換える(過去実行で汚染された (L,M,r) を必ず修復する)
        is_journey = isinstance(t0, str) and (t0.endswith('S33') or t0.endswith('S3A') or t0.endswith('S3B'))
        put(('L', 'M', r), 'M' if (is_journey or t0 is None) else t0)
    # I_{i+1} 左端の起床前保持 (L, L, r) -> L(既存規則の確認、なければ補完)
    for r in ['L', 'www']:
        put(('L', 'L', r), 'L')
    # 凍結起動 CB トリガ (X, L, W) -> CB と、その復帰 (X, CB, W) -> FL を
    # journey 複合左隣にも複製する(n1 = 2 など短縮ケース: 細胞 n1 の左隣が
    # S33 複合表示の瞬間に起動/復帰する)
    for (a, b, c), y in list(rulemap.items()):
        if c != 'W' or b not in ('L', 'CB'):
            continue
        for h in held:
            for ph in ('S33', 'S3A', 'S3B'):
                put((lo[h] + ph, b, c), y)
                put((h + ph, b, c), y)
    # www-lane の残り(管路の左入力がwwwを提示するルールがまだある場合)を
    # L-lane にミラーする。除外: 排出(www,M,L)と journey 複合ターゲット/レーン
    for (a, b, c), y in list(rulemap.items()):
        if a != 'www':
            continue
        if y is None or not isinstance(y, str):
            continue
        if (b, c) == ('M', 'L'):
            continue  # 排出キー
        if y.endswith('S33') or y.endswith('S3A') or y.endswith('S3B'):
            continue
        if b.endswith('S33') or b.endswith('S3A') or b.endswith('S3B'):
            continue
        put(('L', b, c), y)

    # -- R2(1/1 左走行、凍結 I1 のみ、rho(x)=2S+1-x)---------------------------
    for l in fv:
        for v in held:
            put((l, 'F' + v, 'TR'), v + 'R2')        # n1 右端での装填
    fheld = ['www'] + fv
    for r in ['W'] + fv:
        for l in fheld:
            for v in held:
                put((l, 'F' + v, r), 'F' + v)         # R2 未到来の保持
    r2set = [h + 'R2' for h in held]
    # 左走行伝播: トリガは右隣の R2 carrier 表示。l = F_v / www(会合不成立帯)
    for l in fheld:
        for w in held:
            for s in held:
                put((l, 'F' + w, s + 'R2'), w + 'R2')
    # R2 の左隣(通過済み側)の保持
    for l in r2set:
        for w in held:
            for r in ['TR', 'W'] + fv:
                put((l, 'F' + w, r), 'F' + w)
    for l in fheld:
        for v in held:
            for r in ['TR', 'W'] + fv:
                put((l, v + 'R2', r), 'F' + v)         # 1 歩表示後の復帰
    # -- 会合キー(M@2S 判定式 t_dep = S-1+c に一致する唯一の時間一致)----------
    for v in held:
        for w in held:
            for s in held:
                # S 偶数: R2 が会合セルの右隣に来た瞬間(細胞 c-1 は滞在 1 歩目)
                put((v + 'S3A', 'F' + w, s + 'R2'), w + 'NG')
                # S 奇数: R2 が会合セル自身に来た瞬間(細胞 c-1 は滞在 0 歩目)。
            # 会合セル = n1(m1 = n1-1)では右隣は管路の W/L 提示
                for r in fv + ['W', 'L']:
                    put((v + 'S33', w + 'R2', r), w + 'NG')
    # -- NG 会合放出(1/1 右走行)と I2 左端の起動 -----------------------------
    for v in held:
        for w in held:
            for r in ['W', 'TR'] + fv:
                put((v + 'NG', 'F' + w, r), w + 'NG')   # 右走行伝播
                put((v + 'NG', 'L', 'L'), 'M')           # I2 左端の起動
                put((v + 'NG', 'L', 'www'), 'F')         # I2=1 セル区間の起動
                # I2 左端が T3 通過中(未起床=純 T3 / 起床済み=複合)のこともある
                for c2 in ['T3'] + [h2 + 'T3' for h2 in held]:
                    put((v + 'NG', c2, 'L'), 'M')
                    put((v + 'NG', c2, 'www'), 'F')
                # 会合セル(奇数 S)の NG 2 歩表示: 細胞 c-1 の ph2 触発による
                # journey 再点火を防ぐ(ph0 を左隣に持つ NG 表示は存在しない)
                for l in held:
                    put((l + 'S3A', w + 'NG', r), w + 'NG')
    # thaw の FT-lane: 凍結複合表示中に解凍信号 FT が届いたら、その保値の
    # (X, F_v, FT) 準拠の thaw 遷移に落とし込む(cell2@n1 の thaw が STAY 直後
    # に重なるケース)
    for l in (fheld
              + [lo[h] + ph for h in held for ph in phn]
              + [h + ph for h in held for ph in phn]
              + [h + 'NG' for h in held]
              + [h + 'R2' for h in held]):
        for v in held:
            if l.endswith('NG'):
                # NG の左隣はその保値の F 表示と同じ扱い(BNG = F(B) に相当)
                t0 = rulemap.get(('F' + l[0], 'F' + v, 'FT'))
            elif l.endswith('R2'):
                t0 = rulemap.get(('F' + l[0], 'F' + v, 'FT'))
            else:
                t0 = rulemap.get((l, 'F' + v, 'FT'))
            if t0 is None:
                t0 = rulemap.get((l, 'F' + v, 'www'))  # FT2 類似の既知遷移があれば代用
            for ph in phn:
                put((l, lo[v] + ph, 'FT'), t0)
                put((l, v + ph, 'FT'), t0)
        # NG/R2 の左隣は凍結表示 (F_v) のこともある(thaw が重なる配置):
        # 保値に対応する素の (F_l, F_v, FT) thaw 規則の複製
        for v in held:
            t0 = rulemap.get(('F' + l[0], 'F' + v, 'FT')) if                 (l.endswith('NG') or l.endswith('R2')) else                 rulemap.get((l, 'F' + v, 'FT'))
            if t0 is not None:
                put((l, 'F' + v, 'FT'), t0)

    # NG 波の左隣の保持: I2 左端の発射後 M 表示は (NG, M, r) -> M を要求する
    for l in [h + 'NG' for h in held]:
        put((l, 'M', 'L'), 'M')
        put((l, 'M', 'C'), 'M')
        put((l, 'M', 'www'), 'M')
    # NG の表示後の復帰(左隣は氷結複合 ph2 / NG 複合 / 凍結表示 / 境界提示、
    # 右隣は凍結表示のほか次の NG 複合表示もあり得る)
    for l in (fheld + [h + 'S3B' for h in held]
              + [h + 'NG' for h in held]):
        for v in held:
            for r in ['W', 'TR'] + fv + [h + 'NG' for h in held]:
                put((l, v + 'NG', r), 'F' + v)
    ordered = emit(rulemap, list(rulemap.keys()))
    write_file(states, pre, post, ordered)
    print(f'add_meeting: states={len(states)} rules={len(ordered)} '
          f'(overrides={n_override[0]})')

def verify_a1():
    ok = True
    for (n1, m1, n2) in A1_CASES:
        n = n1 + m1 + n2
        gold1_c = [cells(row) for row in golden_base(n1)]
        gold2_c = [cells(row) for row in golden_base(n2)]
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
            gold1_c = [cells(row) for row in golden_base(n1)]
            gold2_c = [cells(row) for row in golden_base(n2)]
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

    # セル番号(全体座標) → (区間種, 区間番号, セル番号)。全体座標 x のセルは
    # t = x-1 に fast 継続(TR)の表示を持つ(将軍の 1/1 波が管路を 1/1 で渡って
    # 各 I_i (i>=2) を未起床のまま通過する。I_{p+1} の壁セル(x = n)では表示
    # なしで T3 に折り返す)。
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
                        # fast 継続(TR): 未起床のまま t = x-1 で右向き 1/1 で通過
                        row.append('TR' if t == seg_x - 1 else 'L')
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
                    # 最終区間 I_{p+1}: fast 継続(TR)の通過と、壁反射の左走行波
                    # T3(未起床セルでは純 T3、起床済みセルでは複合 <X>T3)の戻り。
                    # T2(起床)以降は自らの choreography、その間も T3 波の表示だけ
                    # が上に来る(a1_expected と同じ構造)。
                    x2 = j
                    fb = next((k2 for k2, gr in enumerate(g)
                               if gr[j - 1].strip() != 'L'), None)
                    fb = fb if fb is not None else 10 ** 9
                    t_tr = seg_x - 1
                    t_ft = 2 * n - 1 - seg_x
                    if x2 == n_seg:
                        # 壁セル: TR の通過は表示されず T3 に折り返す
                        if t == t_ft:
                            row.append('T3')
                        elif t < t_st:
                            row.append('L')
                        else:
                            row.append(g[t - t_st][j - 1])
                    elif t == t_tr:
                        row.append('TR')
                    elif t == t_ft:
                        if t < t_st + fb:
                            row.append('T3')
                        else:
                            row.append(g[t - t_st][j - 1].strip() + 'T3')
                    elif t < t_st:
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


# ── A1 (p=1) の全配置スイープ ─────────────────────────────────────────
def sweep_a1():
    # p=1: n=5..16 の全配置 (n1 >= m1)。期待 = SYNCED at 2n-2。
    # 大きい n のスポット + 未定義規則キーの包含確認(check_keys)。
    total = fails = 0
    for n in range(5, 17):
        for n1 in range(1, n - 1):
            for m1 in range(1, n - n1):
                n2 = n - n1 - m1
                if n1 < m1 or n2 < 1:
                    continue
                spec = f'{n1+1}' if m1 == 1 else f'{n1+1}-{n1+m1}'
                out = run(BIN, ['-r', OUT_RULE, '-s', str(n), '-f', spec])
                total += 1
                if f'SYNCED (intact cells) at t={2 * n - 2}' not in out:
                    fails += 1
                    print(f'FAIL n={n} -f {spec} (n1={n1} m1={m1} n2={n2})')
    print(f'A1 sweep n=5..16: {total} layouts, {fails} failures')
    import check_keys  # 遅延(import 順の循環を避ける)
    spots = [(22, 4, 3), (30, 4, 3), (30, 7, 5), (40, 10, 8), (50, 20, 10),
             (50, 5, 1), (44, 3, 2), (36, 9, 9), (50, 45, 2)]
    for (n, n1, m1) in spots:
        spec = f'{n1+1}' if m1 == 1 else f'{n1+1}-{n1+m1}'
        out = run(BIN, ['-r', OUT_RULE, '-s', str(n), '-f', spec])
        ok = f'SYNCED (intact cells) at t={2 * n - 2}' in out
        und = check_keys.check(n, spec, verbose=False)
        if not ok or und:
            print(f'FAIL n={n} n1={n1} m1={m1}: sync={ok}, undefined={len(und)}')
    print(f'A1 spots: {len(spots)} layouts, '
          f'{sum(1 for (n, n1, m1) in spots if check_keys.check(n, f"{n1+1}" if m1 == 1 else f"{n1+1}-{n1+m1}", verbose=False))} with undefined keys')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'verify'
    {'gen': gen, 'discover': discover, 'verify': verify, 'add_a1': add_a1,
     'add_composite': add_composite, 'add_meeting': add_meeting,
     'verify_a1': verify_a1, 'discover_a1': discover_a1,
     'sweep_a1': sweep_a1,
     'sweep_a2': sweep_a2, 'verify_seg': verify_seg,
     'sweep_a3': sweep_a3}[cmd]()