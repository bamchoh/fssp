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
# add_ab の出力(実行用の完全な規則表、約 720 万規則 / 200 MB、git 管理外)。
# OUT_RULE(リポジトリの balzer-freeze.rul.txt)は add_ab の入力 = ②-B までの
# 生成器連鎖(gen→add_a1→add_composite→add_meeting→add_collisions→add_birth)の結果。
FULL_RULE = os.path.join(ROOT, 'balzer-freeze-full.rul.txt')

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


def write_file(states, pre, post, rule_lines, path=None):
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
    with open(path or OUT_RULE, 'w', newline='') as f:
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
    # - NG: 将軍起動信号(故障区間が I_{i+1} 左端の左入力に提示)
    # - W : 故障区間の境界マーカー(左行き線のアイドル提示。www と別の状態にして
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
    # TR: fast 継続信号の対岸横断状態。CB から右行き線 to_right に入った fast 信号を
    # 未起床セル上で 1/1 で運ぶ(起床前のセルだけが保持できる=起床・choreography
    # の前を必ず横断する)。壁で (TR,L,www) -> FT と 0 成本で折り返す。
    if 'TR' not in names:
        states.append(f'{pad("TR")}@101010,00CCAA,others')
        names.append('TR')
    # CB: 境界Carrier(fast信号の故障区間引き渡し)。凍結起動トリガーのステップだけ
    # 境界セルが表示する。左隣からは F(held) と同じに見え、右側には信号を流す。
    if 'CB' not in names:
        states.append(f'{pad("CB")}@101010,0088CC,others')
        names.append('CB')
    additions = [
        (('www', 'L', 'L'), 'L'),    # 区間左端のアイドル(balzer の www 規則にはない)
        (('www', 'L', 'www'), 'L'),  # 1 セル区間(右端)のアイドル
        (('www', 'L', 'W'), 'L',),   # 1 セル中間区間のアイドル(左=www、右=故障区間 W)
        (('NG', 'L', 'L'), 'M'),     # 故障区間の将軍起動信号提示でセルを M へ
        # 起動区間が右端(長さ1)の場合は起動=発火((NG, L, www) のみが目印)
        (('NG', 'L', 'www'), 'F'),
        # n1=1 の縮退区間: FS クランプ時の将軍が FM で凍結し、FT 到着=発火
        # (2n1-2 = 0 なので解凍そのものが射撃。非縮退では FT はセル1の入力に現れない)
        (('www', 'FM', 'FT'), 'F'),
        # A2: 1 セルの中間区間は起動(NG, 左入力)と凍結(FS, 右入力)が同一ステップ
        # に同時提示される(n_i >= 2 では FS が後発なのでこの組み合わせは出ない)
        (('NG', 'L', 'FS'), 'FL'),
        # A2: 1 セル中間区間の解凍=発火(凍結保持値は L、左入力は故障区間の www)
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
        #   - D_i 左端 pickup 経由で CB が対岸の左入力に現れると横断状態 TR に
        #     (未起床セルのみ。L と www の相手を横断する)
        #   - 配列右端(www と隣接)で 0 成本で解凍信号 FT に折り返す
        #   - FT は左走行で未起床セルを戻り、D_i 右端 pickup で to_left に
        #     乗せられて R_i の右入力に提示される(時刻式は往復 1/1 から自動一致)
        #   - 波は 1 セル 1 ステップの提示で進み、すぐもとのアイドルに戻る
        (('CB', 'L', 'L'), 'TR'),
        (('CB', 'L', 'www'), 'FT'),  # 対岸が 1 セル区間: 受容と同時に折り返し
        (('TR', 'L', 'L'), 'TR'),
        (('TR', 'L', 'www'), 'FT'),
        (('TR', 'L', 'W'), 'TR'),  # p>=2: 通過のまま次の故障区間の左端 pickup へ
        # TR 波の通過セルの元のアイドル提示に戻る(左隣は故障区間 www か アイドル L、
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
    # 加えて fast 信号の故障区間引き渡しを担う)。
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
    # FT2 は右端の故障区間だけが提示するので、境界セル以外の既存規則には影響しない。
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
            'a1:', 'time:', 'SYNCED', 'NOT', 'sig:')
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
    # 会合機構入りルールの障害なし走行(凍結ケースの基準。機械自身の
    # S33 排出表示も期待どおりに含むことに注意)
    return state_rows(run(BIN, ['-r', FULL_RULE, '-s', str(n), '-d']))


def golden_base(n):
    # 素の balzer の障害なし走行(A1 の基準 choreography。会合機構は
    # 凍結機構と同居するため、I1 の基準には素のルールセットを使う)
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
                '-r', FULL_RULE, '-s', str(n), '-d',
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
    """Lemma-1 バッテリ(履歴扱い、2026-11-06)。

    ②-A 時点の期待格子(凍結前 gold / 凍結中 F+直前表示 / 解凍後 gold を Δt ずらす)
    で、②-B 以降の機構(journey 表示、担体 CB/cBT/TR、壁反射 T3)を知らないため
    セル照合は全件 NG になる(発火時刻は 14/15 一致。dt=1 だけ本当に解凍しない=
    凍結 1 歩後の解凍という縮退入力)。保証には使わない。現行の保証は
    sweep_a1/a2/a3(定理の主張)と verify_a1(p=1 の構成照合)。MEMO「検証の現状」。
    """
    ok = True
    for (n, t1, dt) in CASES:
        gold = golden_rows(n)
        act = state_rows(run(BIN, [
            '-r', FULL_RULE, '-s', str(n), '-d',
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


_gold2m_cache = {}


def gold2m_cells(n2):
    """I_{i+1} の choreography = 会合機構仕様の障害なし走行(journey 込み)。
    golden_rows(会合ルール集合の障害なし走行)を n2 で実行して返す。"""
    if n2 not in _gold2m_cache:
        _gold2m_cache[n2] = [cells(row) for row in golden_rows(n2)]
    return _gold2m_cache[n2]


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
                # journey の表示は解凍時刻 f_+dt まで(off+dt ∈ [0,2] の交差セルは
                # 解凍遷移で複合が切れて平常表示に復帰する=装置側もそう動く)
                if x <= j_max and t < f_ + dt:
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
                        # 担体が D1 を横断して I_{i+1} 左端に提示される表示は
                        # 専用状態 cBT(②-B)。ride は次セルから素 TR。
                        row.append('cBT' if x2 == 1 else 'TR')
                    elif t >= T2:
                        if n2 == 1 and t == 2 * n - 2:
                            row.append('F')  # 1 セル区間は T2(=2n−2)で即発火
                        else:
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
    text = run(BIN, ['-r', FULL_RULE, '-s', str(n), '-d', '-f', spec])
    return state_rows(text), text


def add_composite():
    # CA 局所化 第 2 段(実装): 壁反射の左走行解凍波 T3 と、起床済みセル上の
    # 複合 carrier <X>T3(chateau+signal)。論文の第 3 反射信号は I_{p+1} を左走行
    # して I_i の右端で解凍を起動する(p=1 では 左行き線(to_left)に回収されて I1 を
    # 解凍)。横断先のセルが起床済み(choreography 表示中)のとき、波の通過で
    # choreography を止めることはできない(I_{p+1} は 2n-2 で無遅延発火する)ので:
    #   - 吸収:  (l, X, <Y>T3) -> <f(l,X,Y)>T3。セルは複合表示になり、自身の
    #            choreography の次状態 f(l,X,Y) を複合の X 部分に保持する。波は
    #            1/1 で進むので時刻に遅延は生じない。
    #   - 放出:  (l, <X>X>T3, r) ではなく (l, <X>T3, r) -> f(l,X,r)。次ステップ
    #            で choreography に戻り、波は左隣へ(左隣は次の吸収を行う)。
    # 未起床(静止 L)セル上では波は純 T3 のまま走る。故障区間の右端 pickup は
    # (rust 側)T3 / 複合を検知すると純 FT に変換して故障区間に乗せる。
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
    #    即時折り返しと、FT2 位相の故障区間がある場合の折り返しも T3 へ)
    rulemap[('TR', 'L', 'www')] = 'T3'
    rulemap[('CB', 'L', 'www')] = 'T3'
    rulemap[('TR', 'L', 'FT2')] = 'T3'
    rulemap[('CB', 'L', 'FT2')] = 'T3'
    # 1b. p >= 2: 横断の D_i 左端・放出の故障区間境界(W 提示)コンテキスト。I_{i+1} が
    #     1 セルでその右隣が次の故障区間の場合、D_i 左端/放出の右入力は W。
    rulemap[('CB', 'L', 'W')] = 'TR'
    for x in ['L', 'www']:
        rulemap[(x, 'TR', 'W')] = 'L'
    # 1c. 両端の衝突: 左入力に CB(区間の送出)が流れていて、右隣に左走行波
    #     (純 T3 / 複合 <Y>T3)が同時にいる場合、表示は横断 TR を優先する
    #     (送出波の旅程が優先。T3 は次ステップで D_i の両端で消える)。
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
    #   R2(反射信号の I1 極性版): D_i 右端(to_left)の TR 装填(rust 側)から 1/1 左走行、
    #     表示 rho(x) = 2S+1-x。会合は時間一致が 4x=2S+2(S 偶数: R2 右隣キー,
    #     c = S/2+1)/4x=2S+3(S 奇数: R2 中央キー, c=(S+3)/2)で排他的に一致する。
    #   NG 会合放出: 会合セルで 1 歩表示(奇数 S は 2 歩: 左隣 ph2 触発の再点火防止)
    #     し、1/1 右走行して I1 右端から D_i 左端(to_right) pickup(rust 側)で故障区間へ。
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

    # -- 右行き線のアイドルは L を提示する(rust 側)。I_{i+1} 左端の
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
    # www-lane の残り(故障区間の左入力がwwwを提示するルールがまだある場合)を
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
    # c = n1(偶数 S)の会合放出: R2 の装填時刻 2S+1−n1 が t_dep と一致し、
    # R2 を会合セルの右隣(故障区間側)に進められないため、装填と同時に
    # journey 位相(S3A、t_dep−1 = 3n1−4 = n1+2m1 の唯一の一致)を左隣に
    # 見る放出キーが担う。n2 = 1 では t_dep−1 の左隣位相が S33 になるうえ
    # ポートを FT が奪うので發火しない(exp も放出を表示しない=一貫)。
    for v in held:
        for w in held:
            put((v + 'S3A', 'F' + w, 'TR'), w + 'NG')
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
            # 会合セル = n1(m1 = n1-1)では右隣は故障区間の W/L 提示
                for r in fv + ['W', 'L']:
                    put((v + 'S33', w + 'R2', r), w + 'NG')
    # -- NG 会合放出(1/1 右走行)と I2 左端の起動 -----------------------------
    for v in held:
        for w in held:
            for r in ['W', 'TR'] + fv:
                put((v + 'NG', 'F' + w, r), w + 'NG')   # 右走行伝播
                put((v + 'NG', 'L', 'L'), 'M')           # I2 左端の起動
                put((v + 'NG', 'L', 'www'), 'F')         # I2=1 セル区間の起動
                # T3 反射波が I2 左端の右隣を通過中の起動(t_ft(x2=2) == 2S−1 の
                # 一族): exp は起動表示が T3 複合(MT3)になる
                put((v + 'NG', 'L', 'T3'), 'MT3')
                # I2 左端が T3 通過中(未起床=純 T3 / 起床済み=複合)のこともある
                for c2 in ['T3'] + [h2 + 'T3' for h2 in held]:
                    put((v + 'NG', c2, 'L'), 'M')
                    put((v + 'NG', c2, 'www'), 'F')
                # 会合セル(奇数 S)の NG 2 歩表示: 細胞 c-1 の ph2 触発による
                # journey 再点火を防ぐ(ph0 を左隣に持つ NG 表示は存在しない)
                for l in held:
                    put((l + 'S3A', w + 'NG', r), w + 'NG')
    # -- 解凍前線の複合左隣レーン(右走り波と解凍前線の 1/1 対面衝突)---------
    # 前線がセル x の解凍時刻 f_x+dt に点火するとき、左隣 x−1 の表示が
    # 右走り波と重なる瞬間がある(x は凍結済、x−1 も凍結済なので重なるのは
    # 氷結複合だけ):
    #   - NG 会合放出 {v}NG: NG と前線が同じ速度で逆向きに走るので
    #     2x == 2n1−2+dt−S(dt <= S)で正面衝突する。exp 設計は前線が勝つ
    #     (NG は x−1 で吸収、x は予定どおり復帰、I_{i+1} は注入代替で起動)。
    #   - S33 journey 氷結複合 {X}ph: journey は会合セル c−1 で止まるので
    #     衝突は x <= c のときだけ(4x ∈ [2n1+dt+1, 2n1+dt+3])。
    # いずれも素の (F_l, F_w, r) 前線規則を複合左隣レーンに複製する
    # (複合の保値 v が素の左隣表示 F_v に対応する)。
    for v in held:
        for w in held:
            for r in held:
                t0 = rulemap.get(('F' + v, 'F' + w, r))
                if t0 is None:
                    continue
                put((v + 'NG', 'F' + w, r), t0)
                for ph in phn:
                    put((v + ph, 'F' + w, r), t0)
    # -- 解凍前線の journey 中央レーン(journey 滞在中に解凍時刻が来る交差)----
    # journey(1/3)と解凍波(1/1 左走り)は逆向きに交差し、交差セル x の解凍
    # 遷移では中央が氷結複合 {X}ph、右隣が既に平常表示(x+1 は先に解凍される)
    # になる。このキー (l, {X}ph, 平常 r) に素の (l, F_X, r) 規則を複製する
    # (r = 平常値は journey 前進/復帰のキー r = 氷結複合と分離しているので
    # 進行への影響はない。S3B 復帰 (l, {X}S3B, r) -> 'F'+X も同形状だが
    # 平常 r のときは交差しか起こらない)。交差セルの解凍は複合表示と重なる
    # (exp も journey 位相を優先表示する)ので、セル自身は複合のまま位相を
    # 進めつつ、遷移値は素の規則に従う。
    for X in held:
        for l in ['www'] + held + fv:
            for r in held:
                t0 = rulemap.get((l, 'F' + X, r))
                if t0 is None:
                    continue
                for ph in phn:
                    put((l, X + ph, r), t0)
    # -- journey 左隣レーン(交差セルの右隣が既に解凍済みのとき)--------------
    # 交差点の右側では解凍が先に起こる(journey は 1/3 で遅い)ので、journey
    # を表示中のセル x の右隣 x+1 は既に平常表示に復帰していることがある。
    # このキー (journey 左隣, 平常中央, 平常右隣) に素の (F_X, w, r) 規則を
    # 複製する(中央が氷結複合のときは上のレーンが担当、進行規則との競合は
    # 平常右隣の分離で避ける)。下位複合(awake セル)は x+1 が解凍済みになる
    # 前に journey が終わるので不要。
    for X in held:
        for w in held:
            for r in held + ['FT2']:
                t0 = rulemap.get(('F' + X, w, r))
                if t0 is None:
                    continue
                for ph in phn:
                    put((X + ph, w, r), t0)
    # -- R2 左隣レーン(n2 = 2: R2 波と解凍前線が同速で並走する一族)------------
    # R2 表示時刻 2S+1−x と前線点火時刻 f_{x+1}+dt は 4−2n2 = 0(n2 = 2)で
    # 全歩行セルで一致する。このキー (sR2, F_w, 平常 r) に素の前線規則
    # (F_s, F_w, r) を複製する(R2 は吸収、セルは予定どおり解凍)。
    for s in held:
        for w in held:
            for r in held:
                t0 = rulemap.get(('F' + s, 'F' + w, r))
                if t0 is None:
                    continue
                put((s + 'R2', 'F' + w, r), t0)
    # -- NG carrier の左隣レーン(2 通り)--------------------------------------
    # (i) 会合セル c_m の右隣が既に解凍済みのとき(放出と右隣の解凍が同時):
    #     carrier の保値 v = 左隣の凍結表示なので素の (F_v, c, r) で置き換える。
    # (ii) 奇数 S の 2 歩表示による故障区間再装填が起動済み I2 左端に当たるとき:
    #     故障区間由来なので素の (L, M, r) と同じ扱い(中央は起動直後の M のみ)。
    for (l, c, r), y in list(rulemap.items()):
        if l != 'L' or c != 'M' or r == 'FT2':
            continue
        for v in held:
            if v + 'NG' in names:
                put((v + 'NG', c, r), y)
    for v in held:
        for c in held:
            if c == 'L' or c == 'M':
                continue
            for r in held + ['FT2']:
                t0 = rulemap.get(('F' + v, c, r))
                if t0 is not None:
                    put((v + 'NG', c, r), t0)
    # -- 複合左隣・解凍後位相(FT2)の保持 --------------------------------------
    # n1 が解凍済みで左隣に carrier/journey 複合が残る配置。素の
    # (F{host}, c, FT2) 保持規則を複合左隣レーンに複製する。
    for l in ([h + 'NG' for h in held] + [h + 'R2' for h in held]):
        for c in held:
            if c == 'L':
                continue
            t0 = rulemap.get(('F' + l[0], c, 'FT2'))
            if t0 is not None:
                put((l, c, 'FT2'), t0)
    # -- 会合放出表示と会合セル自身の解凍の重なり(dt−S 奇数の一族)------------
    # 2 歩表示の 1 歩目が会合セルの解凍遷移と重なるとき(2c_m = 2n1+dt−S−1)、
    # セルの解凍は複合表示のまま規則値に従う(off ∈ [0,2] の交差と同型)。
    # 右隣が既に平常表示(先に解凍)のキーに素の (l-アナログ, F_v, r) を複製。
    # 左隣は journey 複合のこともある(会合直前の位相滞在中の重なり)。
    for X in held:
        for l in (['www'] + held + fv
                  + [lo[h] + ph for h in held for ph in phn]
                  + [h + ph for h in held for ph in phn]
                  + [h + 'NG' for h in held]
                  + [h + 'R2' for h in held]):
            for r in held:
                la = ('F' + l[0].upper()
                      if l.endswith(phn) or l.endswith(('NG', 'R2')) else l)
                t0 = rulemap.get((la, 'F' + X, r))
                if t0 is None:
                    continue
                put((l, X + 'NG', r), t0)
    # thaw の FT-lane: 凍結複合表示中に解凍信号 FT が届いたら、その保値の
    # (X, F_v, FT) 準拠の thaw 遷移に落とし込む(cell2@n1 の thaw が STAY 直後
    # に重なるケース)。複合左隣(NG/R2/journey)は保値対応の素のキー
    # (F{host}, F_v, FT) を引く(journey 交差で n1 と同時刻の解凍になる配置)。
    def ft_l(l, v):
        if l.startswith('www'):
            return rulemap.get((l, 'F' + v, 'FT'))
        host = l[0].upper()
        if l.endswith(phn) or l.endswith(('NG', 'R2')):
            return rulemap.get(('F' + host, 'F' + v, 'FT'))
        # 凍結複合以外(凍結表示そのもの)は素のキー
        t0 = rulemap.get((l, 'F' + v, 'FT'))
        if t0 is None:
            t0 = rulemap.get(('F' + host, 'F' + v, 'FT'))
        return t0
    for l in (fheld
              + [lo[h] + ph for h in held for ph in phn]
              + [h + ph for h in held for ph in phn]
              + [h + 'NG' for h in held]
              + [h + 'R2' for h in held]):
        for v in held:
            if l.startswith('www'):
                t0 = rulemap.get((l, 'F' + v, 'FT'))
                if t0 is None:
                    t0 = rulemap.get((l, 'F' + v, 'www'))  # FT2 類似の既知遷移があれば代用
            else:
                t0 = ft_l(l, v)
            for ph in phn:
                put((l, lo[v] + ph, 'FT'), t0)
                put((l, v + ph, 'FT'), t0)
            # NG 会合放出の表示が解凍 FT の提示と同時になる配置(会合セル = n1、
            # dt = S+1 奇数 S): carrier が FT を踏むので保値の thaw に落とし込む
            if t0 is not None:
                put((l, v + 'NG', 'FT'), t0)
        # NG/R2 の左隣は凍結表示 (F_v) のこともある(thaw が重なる配置):
        # 保値に対応する素の (F_l, F_v, FT) thaw 規則の複製
        for v in held:
            t0 = ft_l(l, v)
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

def add_ab():
    # ②-C (A3 の解凍 = a/b 会合機構、2026-11-06 再設計版):
    #   前進対: b 波 bF(定刻)と a 波 aF(1 歩後ろ)。I_i 右端の CB 入庫で故障区間
    #     D_i 左端が生成する(rust)。未起床セル上を 1/1 で右走し、壁で反射して
    #     bF→bR、aF→T3 になる。b の前進停留(I_{i+1}..I_p 左端で +1)と a の
    #     帰還停留(D_p..D_{i+1} 左端で +1)は D_i 左端の 1 歩遅延線(rust)。
    #   帰還対: bR / T3 は未起床セル(純)・起床済みセル(<X>bw)・凍結セル
    #     (F_v⊕bw)を複合で 1/1 貫通する(不活性)。故障区間が提示する帰還波は
    #     境界マーカー付き WT3 / WbR(D_i の両端 W と区別しない鍵が凍結起動
    #     (C,L,W)→CB を壊すため)。
    #   会合: 自対の D_i 左端で b 帰還の 1 セル後ろに a がいる(rust 判定)
    #     と提示値が abX になり、(l, F_v, abX) が (l, F_v, FT) と同じ解凍。
    #   同一セル衝突: 前進波と帰還波が同じセルに着地する(対向 1/1 で必ず起
    #     きる)ので複合 bF⊕bw / aF⊕bw / CB⊕bw(右端の担体表示と帰還波)。
    #     D_i の両端は複合の「線に入る成分」だけを取り込む(rust 射影)。
    #   生成法: (1) 未起床セル上の波の重ね合わせ規則を直接生成、(2) 既存の
    #     T3 鍵を bR に鏡写し(bR ≡ T3 の挙動)、(3) 解凍鍵 abX、(4) ホスト
    #     (L/X/F_v/CB/W/bF/aF)に波が乗る複合鍵を既存鍵から不動点で複製
    #     (右レーン=吸収、中央=放出、左レーン=通過済み)。既存鍵は上書きしない。
    states, rules, pre, post = load(OUT_RULE)   # 入力 = 基底(②-B まで)
    rulemap = parse_rules(rules)
    names = [state_name(s) for s in states]
    held = ['A', 'B', 'C', 'L', 'M', 'Q', 'R']
    fv = ['F' + h for h in held]   # FB(凍結 B)は状態 0 と同じ名前だが凍結値として実在
    lo = {h: h.lower() for h in held}
    phn = ('S33', 'S3A', 'S3B')
    # journey(1/3 信号)の複合: 起床済みセル lo[X]+ph と凍結セル X+ph の両系。
    # ②-D(A3 の NG 会合機構)では journey 滞在中のセルを他区間の帰還波
    # (T3/bR)が 1/1 で抜けるので、両系とも bw を 1 歩運ぶ複合が要る。
    journeys = ([lo[h] + ph for h in held for ph in phn]
                + [h + ph for h in held for ph in phn])
    uj = [h + ph for h in held for ph in phn]   # 凍結 journey(大文字ホスト)
    bws = ('T3', 'bR')
    fws = ('bF', 'aF')
    # ②-E 重ね合わせ: 前進対 aFbF(停留中の b に次段の a が追いつく)と、その
    # 壁反射 T3bR(帰還対)。起床済み/未起床セルにだけ乗る(凍結区間には入らない:
    # 帰還対は D_p 左端で a が停留して分かれる)。
    fws3 = ('bF', 'aF', 'aFbF')
    bws3 = ('T3', 'bR', 'T3bR')
    # ②-D: R2(第 2 反射、凍結セル上を左走)、{X}NG(会合放出、右走)、故障区間が
    # 折り返す cBT も帰還波と同時刻・同方向に重なるので bw 複合を持つ。
    r2s = [h + 'R2' for h in held]
    ngs = [h + 'NG' for h in held]
    # ②-F: 起床済みセル上を右走する NG 担体 xNG(小文字ホスト。放出 NG が解凍
    # 前線と正面衝突して解凍済み領域を横断する dt <= S 配置)。
    wk = {h: lo[h] + 'NG' for h in held}
    wks = list(wk.values())
    # ②-G: 起床済みセル上を左走する R2 担体 xR2(A1 で I_{p+1} が 1 セルのとき、
    # 壁反射が即時なので折り返し cBT と解凍 FT が同時に届き、R2 が解凍前線と
    # 同じセルを走る)。故障区間の提示値 cBTFT = cBT ⊕ FT。
    wk2 = {h: lo[h] + 'R2' for h in held}
    wk2s = list(wk2.values())
    hosts_plain = (['L'] + [h for h in held if h != 'L'] + fv + ['CB', 'W']
                   + list(fws3) + journeys + r2s + ngs + ['cBT'] + wks + wk2s)

    def plus(h, bw):
        if h == 'L':
            return bw
        if bw == 'T3bR':
            return h + bw if (h in held or h in fws3 or h == 'cBT') else None
        if h in held or h in fv or h in ('CB', 'W') or h in fws3:
            return h + bw
        if h in journeys or h in r2s or h in ngs or h == 'cBT' or h in wks or h in wk2s:
            return h + bw
        if h == 'F':
            return 'F'          # 発火は波を消す(発火時刻に波が重なる鍵の目標)
        return None

    def split(x):
        # 複合名 → (host, bw)。純ホストは (x, None)。
        if x in bws3:
            return ('L', x)
        if x == 'abX':
            return (x, None)
        if x.endswith('T3bR'):
            h = x[:-4]
            if h in held or h in fws3 or h == 'cBT':
                return (h, 'T3bR')
            return (x, None)
        for bw in bws:
            if x.endswith(bw) and len(x) > 2:
                h = x[:-2]
                if h in hosts_plain or h in journeys:
                    return (h, bw)
        return (x, None)

    newn = list(fws) + ['bR', 'abX', 'WT3', 'WbR', 'LbR', 'aFbF', 'T3bR',
                        'aFbFT3', 'aFbFbR', 'aFT3bR', 'bFT3bR', 'cBTT3bR',
                        'aFbFT3bR']
    newn += [h + 'T3bR' for h in held if h != 'L']
    newn += wks + [w + bw for w in wks for bw in bws]
    newn += wk2s + ['cBTFT', 'aFo']   # aFo: D_i 内専用(自分の D_i の a 波マーカー、規則なし)
    for h in ([x for x in held if x != 'L'] + fv + ['CB'] + list(fws) + journeys
              + r2s + ngs + ['cBT']):
        for bw in bws:
            newn.append(h + bw)
    for nm in newn:
        if nm not in names:
            states.append(pad(nm) + '@101010,A0B0E0,others')
            names.append(nm)
    lane_set = set(x.strip() for x in names)
    n_add = [0]

    def put(key, target, keep=True):
        if any(x not in lane_set for x in key) or target not in lane_set:
            return
        if keep and key in rulemap:
            return
        if rulemap.get(key) != target:
            rulemap[key] = target
            n_add[0] += 1

    # ── 1. 未起床セル上の波の重ね合わせ ─────────────────────────────────
    def fw_of(x):
        h, _ = split(x)
        return h if h in fws3 else None

    def bw_of(x):
        return split(x)[1]

    reflect = {'bF': 'bR', 'aF': 'T3', 'aFbF': 'T3bR'}

    def combine(fw, bw):
        if fw is None and bw is None:
            return 'L'
        if bw is None:
            return fw
        if fw is None:
            return bw
        return fw + bw

    comps_fw = [f + bw for f in fws3 for bw in bws3 if f + bw in lane_set]
    xbw = [plus(h, bw) for h in held if h != 'L' for bw in bws]
    # 起床セル複合 <X>bw が左右に来る鍵は choreography の遷移を伴うので
    # ここでは生成せず、既存鍵からの複製(4.)に任せる。
    c_set = ['L'] + list(fws3) + list(bws3) + comps_fw
    l_set = ['L', 'www'] + list(fws3) + list(bws3) + comps_fw
    r_set = (['L', 'www', 'W', 'FT2', 'WT3', 'WbR'] + list(fws3) + list(bws3)
             + comps_fw)
    n1 = 0
    for c in c_set:
        for l in l_set:
            for r in r_set:
                bw = reflect.get(fw_of(c)) if r == 'www' else bw_of(r)
                before = n_add[0]
                put((l, c, r), combine(fw_of(l), bw))
                n1 += n_add[0] - before

    # 右隣の前進波(bF/aF、その複合)は右へ去る=この セルには見えない: 起動
    # 前兆(NG/C/M)や起床セルが右隣に前進波を見る鍵は (l, c, L) と同じ遷移
    # (複合なら帰還成分だけ乗る)。1 セル区間で起動前線が壁反射中の対に追い付く
    # 配置で (NG,L,bFT3) が未定義になった(n=6 -f 2,4 実測)。
    n1b = 0
    for (l, c, r), t in list(rulemap.items()):
        if r != 'L' or (c != 'L' and c not in held and c not in fv):
            continue
        if fw_of(l) is not None or l in bws or l in comps_fw:
            continue   # 未起床セルの波の重ね合わせは上で生成済み
        for f in fws:
            before = n_add[0]
            put((l, c, f), t)
            for bw in bws:
                tt = plus(t, bw) if split(t)[1] is None else None
                if tt:
                    put((l, c, f + bw), tt)
            n1b += n_add[0] - before
    n1 += n1b
    # 1 セル区間(i>=2)の起動: NG 到着時の表示を担体 CB にする(自身が右端。
    # (L,CB,W)→FL で次ステップ凍結、D_i 左端が CB を拾って a/b 対を生成)。
    put(('NG', 'L', 'W'), 'CB')
    # ── 2. 既存 T3 鍵の bR 鏡写し(bR は T3 と同じ挙動で貫通する) ──────────
    def mirror(x):
        h, bw = split(x)
        if bw == 'T3':
            if x == 'LT3':
                return 'LbR'         # 起床複合 <L>T3 は純 T3 と別状態(鏡も別)
            return plus(h, 'bR')     # journey 複合は None → 鍵ごとスキップ
        return x

    n2 = 0
    for (l, c, r), t in list(rulemap.items()):
        if not any('T3' in x for x in (l, c, r, t)):
            continue
        ml, mc, mr, mt = mirror(l), mirror(c), mirror(r), mirror(t)
        if None in (ml, mc, mr, mt):
            continue
        if (ml, mc, mr) == (l, c, r):
            continue
        before = n_add[0]
        put((ml, mc, mr), mt)
        n2 += n_add[0] - before

    # ── 2b. L ホスト複合 LT3/LbR/LT3bR と純 T3/bR/T3bR の同一視 ───────────
    #    未起床の L に波が乗った表示は ②-B 生成器が 'LT3'(woken L ホスト)、
    #    未起床族が 'T3' と名付けたが、意味は同じ(セルは L)。レーンに L+bw を
    #    持つ鍵は純 bw 版の目標に揃える(純版が存在するとき)。放置すると
    #    (M,L,LT3)→T3 のように choreography(C 波の誕生)が落ちる
    #    (実測: A2 n=11 -f 3,6 で I3 が 1 歩遅れる)。
    n2b = 0
    lbw = {'L' + bw: bw for bw in bws3}
    for (l, c, r), t in list(rulemap.items()):
        if not any(x in lbw for x in (l, c, r)):
            continue
        pure = (lbw.get(l, l), lbw.get(c, c), lbw.get(r, r))
        pt = rulemap.get(pure)
        if pt is None or pt == t:
            continue
        before = n_add[0]
        put((l, c, r), pt, keep=False)
        n2b += n_add[0] - before
    # 純 bw レーンの鍵に L+bw 版が無ければ同じ目標で作る(例 (L,TR,T3)→T3 に対する
    # (L,TR,LT3): A2 で 1 セル区間が連続し fast 継続 TR が複数本走ると、先頭の
    # 壁反射エコーが後続 TR と対面する。実測 A2 n=13 -f 3,5,7,9)。
    inv = {bw: 'L' + bw for bw in bws3}
    for (l, c, r), t in list(rulemap.items()):
        lanes = (l, c, r)
        if not any(x in inv for x in lanes):
            continue
        for i in range(3):
            if lanes[i] in inv:
                k2 = list(lanes)
                k2[i] = inv[lanes[i]]
                k2 = tuple(k2)
                if k2 not in rulemap:
                    before = n_add[0]
                    put(k2, t)
                    n2b += n_add[0] - before

    # ── 3. 解凍鍵: (l, F_v, abX) = (l, F_v, FT) ─────────────────────────
    n3 = 0
    for (l, c, r), t in list(rulemap.items()):
        if r != 'FT':
            continue
        before = n_add[0]
        put((l, c, 'abX'), t)      # 中央は F_v のほか journey/R2/NG 付き凍結値も
        if c in fv:
            for bw in bws:
                # 自身が運ぶ帰還波は左へ去るので解凍先に波は付けない
                # ((L,FMbR,MbR) 未定義= FLbR→MbR の幽霊波、n=9 -f 3,5,8 実測)
                put((l, c + bw, 'abX'), t)
        n3 += n_add[0] - before

    # ── 3a. ②-E 1 セル区間の journey(会合機構を 1 セル区間にも適用する) ──
    #    i>=2: 起動 NG で CB を表示した次ステップから凍結 journey LS33→LS3A→
    #          LS3B→FL(左入力は故障区間アイドル L、または 2 歩提示の 2 歩目 NG)。
    #    i=1 : 将軍 M の右が故障区間(W)なら t=0 に凍結+journey MS33→…→FM。
    #          D_i 左端は初回 pickup の M を担体として拾う(rust)。
    n3a = 0
    for key, tgt in [(('L', 'CB', 'W'), 'LS33'), (('NG', 'CB', 'W'), 'LS33'),
                     (('LNG', 'CB', 'W'), 'LS33'), (('www', 'M', 'W'), 'MS33'),
                     # A2: 1 セル区間に担体 CB が提示されたときの横断表示(右が故障区間 W)。
                     # 折り返し(R2 装填)の判定表示で、fast 継続は次の D_i 左端が
                     # cBT 表示を TR として拾う(rust)。表示後は L に戻る。
                     (('CB', 'L', 'W'), 'cBT'), (('L', 'cBT', 'W'), 'L')]:
        before = n_add[0]
        put(key, tgt, keep=False)
        n3a += n_add[0] - before

    # ── 3c. 起動 NG と帰還波の同時到着: 起動が勝つ。(NG|LNG|{X}NG, L, L)→M の
    #    右レーンを未起床セル上の波(T3/bR/T3bR と L ホスト複合)に広げ、目標に
    #    波を乗せる(M⊕bw)。add_collisions の (NG, L, T3族)→T3(通過)を上書き
    #    する: D_i 左端の会合を A2 でも有効にしたことで、fast 波の T3 エコーが
    #    起動 NG と同時に I_{i+1} 左端に来る配置が現れた(実測 n=10 -f 3,6)。
    n3c = 0
    ng_lanes = ['NG', 'LNG'] + [h + 'NG' for h in held] + wks
    for lng in ng_lanes:
        t0 = rulemap.get((lng, 'L', 'L'))
        if t0 is None and lng in wks:
            # 起床済み担体の起動キーは凍結担体 {X}NG と同じ((xNG,L,L)→M 等)
            src = lng[0].upper() + 'NG'
            for r in ('L', 'www', 'W', 'FT2'):
                t = rulemap.get((src, 'L', r))
                if t is not None:
                    put((lng, 'L', r), t)
            t0 = rulemap.get((lng, 'L', 'L'))
        if t0 is None:
            continue
        # fast 波の継続 TR が右隣にいる瞬間の起動(n_i=1 かつ m_i=1: TR が 1 歩先)
        put((lng, 'L', 'TR'), t0)
    # TR(右走 fast 継続)は未起床 L の右隣にいても透明: (l, L, TR) = (l, L, L)
    for (l, c, r), t in list(rulemap.items()):
        if c == 'L' and r == 'L' and l not in ('L', 'www'):
            before = n_add[0]
            put((l, 'L', 'TR'), t)
            n3c += n_add[0] - before
    # 実験 ②-G: n_{i+1}=1 の担体表示を一般形に揃える(cBT 表示→次ステップ壁反射)
    if os.environ.get('FSSP_CBT1'):
        put(('CB', 'L', 'www'), 'cBT', keep=False)
        put(('L', 'cBT', 'www'), 'T3', keep=False)
        for bw in bws3:
            for r in (bw, 'L' + bw):
                tt = plus(t0, bw)
                if tt is None:
                    continue
                before = n_add[0]
                put((lng, 'L', r), tt, keep=False)
                n3c += n_add[0] - before

    # ── 3e. ②-F 会合放出と解凍の同時: R2 が解凍前線の 1 歩先を走る配置
    #    (i=p、n_{p+1}=1)では、会合セルの右隣が放出のステップに既に解凍済み。
    #    会合鍵 (l, c, r)→wNG の右レーンを起床済み状態に広げ、目標は「解凍した
    #    値に NG を乗せた担体」(xNG)にする。
    n3e = 0
    emit_keys = [(k, t) for k, t in rulemap.items() if t in ngs and k[2] in ['W', 'L'] + fv]
    for (l, c, r), t in emit_keys:
        hc, bc = split(c)
        base_c = ('F' + hc[0]) if (hc.endswith('R2') and hc[0] in held) else hc
        if base_c not in fv:
            continue
        for r2 in held + ['abX', 'FT']:
            t2 = rulemap.get((l, base_c, r2))
            if t2 is None and r2 in ('abX', 'FT') and l in uj:
                t2 = rulemap.get(('F' + l[0], base_c, r2))   # journey レーン = 凍結値
            if t2 is None:
                continue
            ht2, bt2 = split(t2)
            if ht2 not in held:
                continue
            nt = lo[ht2] + 'NG'
            if bt2:
                nt = nt + bt2 if (nt + bt2) in lane_set else None
            if nt is None:
                continue
            before = n_add[0]
            put((l, c, r2), nt, keep=False)
            n3e += n_add[0] - before

    # ── 3f. ②-G 解凍と R2 装填の同時(cBTFT)、起床済み R2 担体との会合 ──────
    def r2w_target(t):
        ht, bt = split(t)
        if ht == 'F':
            return 'F'
        if ht in held and bt is None:
            return wk2[ht]
        return None
    n3f = 0
    uj = [h + ph for h in held for ph in phn]
    r2set = [h + 'R2' for h in held]
    # 3g. 凍結 journey セルの右隣が起床済み(前線が会合前に通過、または会合と解凍
    #     の同時): journey を落として凍結値の解凍遷移に従う。
    for (l, c, r), t in list(rulemap.items()):
        if c not in fv or r not in held:
            continue
        for ph in phn:
            jc = c[1] + ph
            if jc not in lane_set:
                continue
            before = n_add[0]
            put((l, jc, r), t)                 # 前線が通過: journey を落とす
            put((l, jc, wk2[r]), t)            # 右隣で会合(R2 消費)しつつ解凍
            put((l, jc, wk[r]), t)             # 右隣の NG(右走)は無関係
            n3f += n_add[0] - before
    # 奇数 S の会合鍵 (vS33, wR2, r)→wNG の右レーンに FT2(解凍後の故障区間アイドル。
    # A1 で I_{p+1} が 1 セルのときは R2 装填と同時に解凍位相に入る)を加える。
    for (l, c, r), t in list(rulemap.items()):
        if r == 'W' and c in r2set and t in ngs:
            before = n_add[0]
            put((l, c, 'FT2'), t)
            n3f += n_add[0] - before
    # cBTFT = cBT ⊕ FT: cBT の効果(R2 装填 / 会合放出 / journey 続行)と解凍を合成
    for (l, c, r), t_cbt in list(rulemap.items()):
        if r != 'cBT':
            continue
        is_j = c in uj
        base_c = ('F' + c[0]) if is_j else c
        if base_c not in fv:
            continue
        t_ft = rulemap.get((l, base_c, 'FT'))
        if t_ft is None:
            continue
        ht, bt = split(t_ft)
        if t_cbt in r2set:
            nt = r2w_target(t_ft)
        elif t_cbt in ngs:
            nt = wk[ht] if (ht in held and bt is None) else None
        else:
            nt = t_ft
        if nt is None:
            continue
        before = n_add[0]
        put((l, c, 'cBTFT'), nt, keep=False)
        n3f += n_add[0] - before
    for (l, c, r), t in list(rulemap.items()):
        if t not in ngs:
            continue
        # 偶数 S: 右隣の R2 が起床済み担体 → 中央は解凍しつつ放出(xNG)
        if r in r2set and c in fv:
            s_ = r[0]
            th = rulemap.get((l, c, s_))
            if th is not None and split(th)[0] in held and split(th)[1] is None:
                before = n_add[0]
                put((l, c, wk2[s_]), wk[split(th)[0]])
                n3f += n_add[0] - before
        # 奇数 S: 中央の R2 が起床済み担体 → 放出しつつ中央は起床済みの遷移(xNG)。
        # 右隣は凍結(W/fv)のほか解凍済み(held)も取る(前線と同走する R2)。
        if c in r2set:
            w_ = c[0]
            for r2 in [r] + held:
                th = (rulemap.get((l, w_, r2)) or rulemap.get(('F' + l[0], w_, r2))
                      or rulemap.get((l[0], w_, r2)))
                if th is not None and split(th)[0] in held and split(th)[1] is None:
                    before = n_add[0]
                    put((l, wk2[w_], r2), wk[split(th)[0]])
                    n3f += n_add[0] - before

    # ── 3b. ②-E D_i 左端の会合: 会合セルが左端セルに落ちる対では cBT(第 2
    #    反射)が I_i 右端の journey 滞在中に提示される。R2 は装填せず(会合は
    #    D_i 左端セルが行う)、journey はアイドル W を見たときと同じ進行をする。
    n3b = 0
    uj = [h + ph for h in held for ph in phn]
    for (l, c, r), t in list(rulemap.items()):
        if r == 'W' and c in uj:
            before = n_add[0]
            put((l, c, 'cBT'), t)
            n3b += n_add[0] - before

    # ── 4. ホストに波が乗る複合鍵の不動点複製 ───────────────────────────
    carriers = set(hosts_plain) | set(bws3) | set(comps_fw) | set(xbw) \
        | set(v + bw for v in fv for bw in bws) | set('CB' + bw for bw in bws)
    n4 = 0
    for _round in range(6):
        added = 0
        for (l, c, r), t in list(rulemap.items()):
            cands = []
            hl, bl = split(l)
            hc, bc = split(c)
            hr, br_ = split(r)
            ht, bt = split(t)
            # (A) 右レーンの吸収: r が素ホストで、中央が波を運べるとき
            if br_ is None and hr in hosts_plain and c in carriers:
                for bw in bws3:
                    nr = plus(hr, bw)
                    nt = plus(ht, bw) if bt is None else (t if bt == bw else None)
                    if nr and nt:
                        cands.append(((l, c, nr), nt))
            # (B) 中央の放出: 中央が素ホストなら複合でも同じ遷移
            if bc is None and hc in hosts_plain and hc != 'W':
                for bw in bws3:
                    nc = plus(hc, bw)
                    if nc:
                        cands.append(((l, nc, r), t))
            # (C) 左レーンの通過済み
            if bl is None and hl in hosts_plain and hl != 'W':
                for bw in bws3:
                    nl = plus(hl, bw)
                    if nl:
                        cands.append(((nl, c, r), t))
            # ②-F 右走 NG 担体の族(bw 族の鏡像): NG は左隣から入り(A')、中央から
            # 出て(B')、右隣に乗っていても自身の遷移は変わらない(C')。
            #   ホストの対応: 起床済み X ↔ xNG、凍結 FX ↔ XNG(既存の凍結担体)。
            def fw_host(x):
                # レーン値 → (NG を外したホスト, NG 付きの名前)。無ければ None。
                hx, bx = split(x)
                if hx in held:
                    return (x, plus(wk[hx], bx) if bx else wk[hx])
                if hx in fv and hx != 'FL':
                    return (x, plus(hx[1] + 'NG', bx) if bx else hx[1] + 'NG')
                return None
            def fw_target(t):
                ht, bt = split(t)
                if ht == 'F':
                    return 'F'
                if ht in held:
                    return plus(wk[ht], bt) if bt else wk[ht]
                if ht in fv and ht != 'FL':
                    return plus(ht[1] + 'NG', bt) if bt else ht[1] + 'NG'
                return None
            if hc in held or hc in fv:
                fl = fw_host(l)
                if fl is not None and fl[1]:
                    nt = fw_target(t)
                    if nt:
                        before = n_add[0]
                        # 起床済み中央は上書き(旧設計「前線が勝つ」= NG を吸収する
                        # (XNG, 起床済み, r)→t を、NG を乗せて運ぶ遷移に置換する)
                        # 常に上書き: 旧設計「前線が勝つ」(NG を吸収する遷移)を、
                        # NG を乗せて運ぶ遷移に置換する(凍結目標なら既存と同値)
                        put((fl[1], c, r), nt, keep=False)
                        added += n_add[0] - before
            # R2 担体(凍結 FX ↔ XR2): 中央の放出と左レーンの通過済みは凍結値と
            # 同じ遷移(解凍前線の直前を走る R2 が前線に追われて解凍するとき等)
            if hc in fv and hc != 'FL' or hc == 'FL':
                nc = hc[1] + 'R2'
                if bc is None and nc in lane_set:
                    before = n_add[0]
                    put((l, nc, r), t)
                    added += n_add[0] - before
            if hl in fv and bl is None:
                nl = hl[1] + 'R2'
                if nl in lane_set:
                    before = n_add[0]
                    put((nl, c, r), t)
                    added += n_add[0] - before
            fc = fw_host(c)
            if fc is not None and fc[1]:
                before = n_add[0]
                put((l, fc[1], r), t)
                # 左隣も担体(会合セルの NG 2 歩表示の 2 歩目): 既に乗っている NG と
                # 同じものなので吸収しない=中央の放出と同じ遷移
                fl2 = fw_host(l)
                if fl2 is not None and fl2[1]:
                    put((fl2[1], fc[1], r), t, keep=False)
                added += n_add[0] - before
            fr = fw_host(r)
            if fr is not None and fr[1]:
                before = n_add[0]
                put((l, c, fr[1]), t)
                added += n_add[0] - before
            # ②-G 起床済み R2 担体 xR2(左走): (A) 右隣の xR2 を吸収(中央は起床済み
            # または解凍中の凍結セル)、(B) 中央の放出、(C) 左レーンは通過済み
            if hr in held and br_ is None and (hc in held or hc in fv) and bc is None:
                nt = r2w_target(t)
                if nt:
                    before = n_add[0]
                    put((l, c, wk2[hr]), nt)
                    added += n_add[0] - before
            if hc in held and bc is None and c not in ('L',):
                before = n_add[0]
                put((l, wk2[hc], r), t)
                added += n_add[0] - before
            if hl in held and bl is None and l not in ('L',):
                before = n_add[0]
                put((wk2[hl], c, r), t)
                added += n_add[0] - before
            for k, tt in cands:
                before = n_add[0]
                put(k, tt)
                added += n_add[0] - before
        n4 += added
        if added == 0:
            break
    ordered = emit(rulemap, list(rulemap.keys()))
    write_file(states, pre, post, ordered, path=FULL_RULE)   # 出力 = 完全規則表
    print(f'add_ab: states={len(states)} rules={len(ordered)} '
          f'(+{n_add[0]}: asleep {n1}, mirror {n2}, Lbw-unify {n2b}, thaw {n3}, '
          f'1cell {n3a}, launch+wave {n3c}, meet+thaw {n3e}, r2w {n3f}, pipe-meet {n3b}, clone {n4})')


def add_collisions():
    # ②-B 衝突族: 対の担体(CB)横断と fast 波の壁反射帰り T3 が短い I_{i+1} 内で
    # 重なるとき、担体の 1 歩 TR 表示((CB,L,L)→TR)を優先する(帰り T3 は
    # p>=2 では解凍に使わない=注入代替なので消えてよい)。欠落キーは
    # state0(FB) に落ちて choreography を壊すため必須。
    states, rules, pre, post = load(OUT_RULE)
    rulemap = parse_rules(rules)
    names = set(state_name(x).strip() for x in states)
    n_add = 0
    # 専用状態 cBT: 対の担体(CB)が D_i を横断して I_{i+1} 左端に提示されるとき
    # の表示。素の TR だと D_i 右端 pickup の (F_l,F_v,TR)→{v}R2 変換が fast 波
    # ride の TR にも無差別発火して偽 R2 誕生(実測 (2,1,5,3,2) n=13)。
    # cBT だけを D_i 右端 pickup(own_wave ゲート)が拾うので区別できる。
    if 'cBT' not in names:
        states.append(pad('cBT') + '@101010,E0C040,others')
        names.add('cBT')
    # 横断表示: (CB-stream, x, y) → cBT(x = 就寝 L / T3 帰り波、y = L / www /
    # T3 族)。素の (CB,L,L)→TR と 1c 族((CB,L,y)→TR)を置換。
    # (CB, T3, y) のみ変換(壁隣接 (CB,L,www) は committed の →T3 =
    # 「n_{i+1}=1 で担体が即壁反射する」機構)。
    # 1c 族((CB,L,y)→TR、y=T3族)も cBT に置換(横断表示の統一)。
    # ※add_collisions は再実行可能にするため、committed 値への逆上書きも
    #   明示する(以前の誤置換 (CB,L,www)→cBT が残っている場合の修復)。
    rulemap[('CB', 'L', 'www')] = 'T3'
    rulemap.pop(('CB', 'T3', 'www'), None)
    for y in ['L', 'T3'] + ['%sT3' % x for x in 'ABCLMQR']:
        for x in ('L', 'T3'):
            key = ('CB', x, y)
            if rulemap.get(key) != 'cBT':
                rulemap[key] = 'cBT'
                n_add += 1
    # ride 継続(左端の次セルから素 TR で 1/1 右走)と表示後の復帰
    # (復帰は右隣が L のとき www=壁隣接(n_{i+1}=1 縮退)の 2 変種)
    for key, tgt in [(('cBT', 'L', 'L'), 'TR'), (('L', 'cBT', 'L'), 'L'),
                     (('L', 'cBT', 'www'), 'L')]:
        if rulemap.get(key) != tgt:
            rulemap[key] = tgt
            n_add += 1
    # fast 波の壁反射帰り T3 が対の担体の TR 表示と隣接した衝突:
    #  - T3 帰り波が asleep セルに入るとき左の担体表示(TR/NG 系)を左入力に
    #    持つキーが欠落 → state0(FB) 蔓延で I3 が死ぬ(実測 (3,1,3,1,4) n=12)。
    #    (l, L, T3族) -> T3 に担体系の l を追加(既存族は www/L+interior のみ)。
    for l in ('TR', 'NG', 'LNG'):
        for y in ['T3'] + ['%sT3' % x for x in 'ABCLMQR']:
            key = (l, 'L', y)
            if key not in rulemap:
                rulemap[key] = 'T3' if y == 'T3' else y
                n_add += 1
    # r-lane TR の cBT 鏡写し: to_left ストリームの R2 装填/放出トリガ
    # ((F_l,F_v,TR)→{v}R2、(X+S3A,F_v,TR)→放出 等)は cBT を入力に取る
    # (cBT 専用 pickup の後、素 TR は to_left に届かないため実質引継ぎ)。
    n_mirror = 0
    for (l, c, r), t in list(rulemap.items()):
        if r == 'TR':
            key = (l, c, 'cBT')
            if rulemap.get(key) != t:
                rulemap[key] = t
                n_mirror += 1
        if l == 'TR':
            key = ('cBT', c, r)
            if rulemap.get(key) != t:
                rulemap[key] = t
                n_mirror += 1
    # cBT を L と同様に扱う鏡写し(就寝セルに T3 帰り波が入る時、中央が
    # cBT(担体横断表示の残像)だと (x,cBT,T3族) が欠落 → state0(FB) 蔓延:
    # 実測 (2,1,3,1,3) n=10)。(x, L, T3族)→T3 を (x, cBT, T3族)→T3 へ。
    for (l, c, r), t in list(rulemap.items()):
        if c == 'L' and r == 'T3':
            key = (l, 'cBT', r)
            if rulemap.get(key) != t:
                rulemap[key] = t
                n_mirror += 1
    bad = [k for k in rulemap for lane in k if lane.strip() not in names]
    if bad:
        raise SystemExit(f'add_collisions: unknown-lane keys: {bad[:5]}')
    ordered = emit(rulemap, list(rulemap.keys()))
    write_file(states, pre, post, ordered)
    print(f'add_collisions: states={len(states)} rules={len(ordered)} '
          f'(+{n_add}, mirror {n_mirror})')


def add_birth():
    # ②-B 誕生+衝突族(2026-10-17 exp_birth 実験の緑化結果を本線へ):
    #   1. 誕生キー (NG|LNG, M, L) -> mS33。故障区間中継の NG 2 歩提示(rust 側
    #      ng_hold)の 2 歩目に左入力 LNG/NG を見て I_{i+1} の journey を誕生
    #      させる。素の (L,M,L) は I1 解凍後 ordinary choreography でも起こる
    #      ので絶対に置換しない。
    #   2. T3 エコー × journey 複合の衝突族(fix-point):
    #      - 左レーン <X+ph>T3: 左隣の偽装(同 target)
    #      - 中心    <X+ph>T3: 波が抜けた後の普通の進行(同 target)
    #      - 右レーン <r>T3:   吸収(target の T3 付き表示)。
    #        就寝セル(中心 L)は除外(純 T3 貫通の既存族を壊さない)
    #      新状態 = lo[h]+ph+T3(21): journey 滞在中のセルが T3 を 1 歩運ぶ
    #      carrier(D_i 右端 pickup は rust 側で 6 文字小文字 T3 複合を認識する)。
    states, rules, pre, post = load(OUT_RULE)
    rulemap = parse_rules(rules)
    names = [state_name(s) for s in states]
    held = ['A', 'B', 'C', 'L', 'M', 'Q', 'R']
    lo = {h: h.lower() for h in held}
    phn = ('S33', 'S3A', 'S3B')
    interior = held
    # 実在状態名(未知レーンを書くとバイナリのパーサが黙って idx をずらし、
    # 規則表を全面的に壊す)
    lane_set = set(x.strip() for x in names)

    n_rep = n_add = 0
    for (lhs, tgt) in [(('NG', 'M', 'L'), 'mS33'), (('LNG', 'M', 'L'), 'mS33')]:
        cur = rulemap.get(lhs)
        if cur != 'mS33':
            if lhs in rulemap:
                n_rep += 1
            else:
                n_add += 1
            rulemap[lhs] = 'mS33'

    jcomps = [lo[h] + ph for h in held for ph in phn]
    jcarr = [lo[h] + ph + 'T3' for h in held for ph in phn]
    for nm in jcarr:
        if nm not in names:
            states.append(pad(nm) + '@101010,B070C0,others')
            names.append(nm)
    lane_set = set(x.strip() for x in (state_name(s) for s in states))

    def t3ify(t):
        t = t.strip()
        if t in interior or t in jcomps:
            return t + 'T3'
        return None

    n_fp = 0
    for _round in range(8):
        added = 0
        snap = list(rulemap.items())
        for (l, c, r), t in snap:
            l_, c_, r_ = l.strip(), c.strip(), r.strip()
            lt = l_ + 'T3'
            ct = c_ + 'T3' if (c_ in interior or c_ in jcomps) else None
            rt = 'T3' if r_ == 'L' else (r_ + 'T3' if r_ != 'T3' else None)
            if lt in lane_set:
                key = (lt, c_, r_)
                if rulemap.get(key) != t:
                    rulemap[key] = t
                    added += 1
            if ct is not None and ct in lane_set:
                key = (l_, ct, r_)
                if rulemap.get(key) != t:
                    rulemap[key] = t
                    added += 1
            if rt is not None and rt in lane_set:
                if c_ == 'L':
                    continue  # 就寝セル上の波は純 T3 貫通(既存族)
                t2 = t3ify(t)
                if t2 is None:
                    continue
                key = (l_, c_, rt)
                if rulemap.get(key) != t2:
                    rulemap[key] = t2
                    added += 1
        if added == 0:
            break
        n_fp += added

    # 起動×波同時(誕生セル D=0/−1 の 2 系)
    n_launch = 0
    for l in ('NG', 'LNG'):
        if rulemap.get((l, 'M', 'T3')) != 'mS33T3':
            rulemap[(l, 'M', 'T3')] = 'mS33T3'
            n_launch += 1
        if rulemap.get((l, 'MT3', 'L')) != 'mS33':
            rulemap[(l, 'MT3', 'L')] = 'mS33'
            n_launch += 1
    # 発火時の複合解決の補完(fire ->F キーの複合レーン展開。guard 付きで
    # 追加のみ: 既存 target を壊さない)
    comps = [lo[h] + p for h in held for p in phn] + [h + p for h in held for p in phn]
    n_fire = 0
    fire_keys = [(k, tgt) for k, tgt in rulemap.items() if tgt == 'F']
    for ((l, X, r), tgt) in fire_keys:
        cv = lo.get(X.strip())
        if cv is not None:
            for v in (cv + p for p in phn):
                if (l.strip(), v, r.strip()) not in rulemap:
                    rulemap[(l.strip(), v, r.strip())] = 'F'
                    n_fire += 1
        for v in comps:
            if (v, X.strip(), r.strip()) not in rulemap:
                rulemap[(v, X.strip(), r.strip())] = 'F'
                n_fire += 1
            if (l.strip(), X.strip(), v) not in rulemap:
                rulemap[(l.strip(), X.strip(), v)] = 'F'
                n_fire += 1
    # 最終レーン検査
    bad = [(k) for k in rulemap for lane in k if lane not in lane_set]
    if bad:
        raise SystemExit(f'add_birth: unknown-lane keys: {bad[:5]}')
    ordered = emit(rulemap, list(rulemap.keys()))
    write_file(states, pre, post, ordered)
    print(f'add_birth: states={len(states)} rules={len(ordered)} '
          f'(birth +{n_rep}rep/{n_add}add, fixpoint {n_fp}, launch {n_launch}, '
          f'fire {n_fire})')


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
                a, e = a.strip(), e.strip()
                # ②-F/②-G: 起床済みセル上の担体 xNG / xR2(dt <= S で放出 NG が
                # 解凍前線を越えて走る等)は表示上ホストに重なるだけ(choreography
                # はホストのもの)。照合はホストで行う。
                if len(a) == 3 and a[0].islower() and a[1:] in ('NG', 'R2'):
                    a = a[0].upper()
                if a != e:
                    bad += 1
                    if first is None:
                        first = (t, k + 1, a, e)
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
                # 不一致行の直前のアクティブ行+ D_i 提示から規則キーを組み立てる
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
        return run(BIN, ['-r', FULL_RULE, '-s', str(n), '-f', spec])

    def check(n, spec, segs, fails):
        fails[1] += 1
        out = a2_run(n, spec)
        p_ = spec.count(',') + 1
        if (f'SYNCED (intact cells) at t={2 * n - 2}' not in out
                or f'injected signals = {p_}' not in out):
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
    # t = x-1 に fast 継続(TR)の表示を持つ(将軍の 1/1 波が故障区間を 1/1 で渡って
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
    """A2/A3 の行単位照合(履歴扱い、2026-11-06)。

    seg_expected は ②-B 以降の機構(journey 表示、担体 cBT、a/b 波 aF/bF/bR、LNG)
    を知らないためセル照合は全件 NG になる(発火時刻は 7/7 一致)。保証には使わない。
    構成照合が必要になったら verify_a1 の正規化を p>=2 に広げた射影オラクル
    (機構の重なりを剥がしてホストを素の Balzer の t_i ずらし・Δt_i 凍結と照合、
    故障セルは無視)に置き換える。MEMO「検証の現状」。
    """
    ok = True
    for (a3, n, spec, sizes) in seg_mode_cases():
        if a3_only and not a3:
            continue
        p = (len(sizes) - 1) // 2
        golds = [[cells(r) for r in golden_rows(sz)]
                 for idx_i, sz in [(k, sizes[k]) for k in range(0, len(sizes), 2)]]
        flag = ['--a3'] if a3 else []
        act = state_rows(run(BIN, ['-r', FULL_RULE, '-s', str(n), '-d', '-f', spec] + flag))
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
                            '-r', FULL_RULE, '-s', str(n),
                            '-f', f'{a1}-{b1},{a2}-{b2}', '--a3'])
                        if (f'SYNCED (intact cells) at t={2 * n - 2 + 2}' not in out
                                or 'injected signals = 0' not in out):
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
                                    '-r', FULL_RULE, '-s', str(n),
                                    '-f', spec3, '--a3'])
                                if (f'SYNCED (intact cells) at t={2 * n - 2 + 3}' not in out
                                        or 'injected signals = 0' not in out):
                                    fails += 1
                                    print(f'FAIL p3 n={n} -f {spec3} (sizes={sizes})')
    print(f'A3 sweep: {total} layouts, {fails} failures (p=2 + p=3; SYNCED at 2n-2+p and 0 injections)')


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
                out = run(BIN, ['-r', FULL_RULE, '-s', str(n), '-f', spec])
                total += 1
                if (f'SYNCED (intact cells) at t={2 * n - 2}' not in out
                        or 'injected signals = 0' not in out):
                    fails += 1
                    print(f'FAIL n={n} -f {spec} (n1={n1} m1={m1} n2={n2})')
    print(f'A1 sweep n=5..16: {total} layouts, {fails} failures (SYNCED at 2n-2 and 0 injections)')
    import check_keys  # 遅延(import 順の循環を避ける)
    spots = [(22, 4, 3), (30, 4, 3), (30, 7, 5), (40, 10, 8), (50, 20, 10),
             (50, 5, 1), (44, 3, 2), (36, 9, 9), (50, 45, 2)]
    for (n, n1, m1) in spots:
        spec = f'{n1+1}' if m1 == 1 else f'{n1+1}-{n1+m1}'
        out = run(BIN, ['-r', FULL_RULE, '-s', str(n), '-f', spec])
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
     'add_ab': add_ab,
     'verify_a1': verify_a1, 'discover_a1': discover_a1,
     'sweep_a1': sweep_a1,
     'sweep_a2': sweep_a2, 'verify_seg': verify_seg,
     'sweep_a3': sweep_a3}[cmd]()