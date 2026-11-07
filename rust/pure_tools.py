# -*- coding: utf-8 -*-
"""(a) 純粋シフト線モード(--pure、A3 限定、n_i > m_i)の規則生成器。

故障セルは生の状態を 1/1 で写すだけ(判断なし)。従来 rust の故障区間処理
(射影・遅延線・会合判定・1 歩記憶・2 歩提示)は全て境界セル(I_i 右端 = 右隣が
故障、I_{i+1} 左端 = 左隣が故障)の規則に移す。出力は balzer-freeze-pure.rul.txt:

  通常の規則行          内部セルと、境界セルの既定(射影後の入力で引く)
  PL:a->b               左端セルが左入力(I_i 右端の生の状態)をどう読むか(信号の分類)
  PR:a->b               右端セルが右入力(I_{i+1} 左端の生の状態)をどう読むか
  PJ:a->b               内部セルが境界専用状態を入力に見るときの基底への射影
  B1:l##c##r->t         左端セル専用の規則(射影後の l、生の c、r)
  B2:l##c##r->t         右端セル専用の規則(生の l、c、射影後の r)

設計は golang/MEMO.md「(a) 純粋シフト線・A3 限定・n_i > m_i の設計検討」。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import freeze_tools as F  # noqa: E402
from freeze_tools import load, parse_rules, state_name, pad, write_file, FULL_RULE, ROOT  # noqa: E402

PURE_RULE = os.path.join(ROOT, 'balzer-freeze-pure.rul.txt')

HOLD = 'T3h'      # 右端が帰還 a(T3)を 1 歩保持する状態の接尾
THAWED = '~'      # 解凍済み右端の状態族の接尾(右入力 W を壁として扱う=旧 FT2 レーン)


def fwd(x):
    """前進成分(a/b 波)。無ければ None。"""
    if x.startswith('FLbF'):
        return 'bF2'      # 誕生の b は停留しない=本物として届ける(帰還波との複合も)
    if x.startswith('bF2'):
        return 'bF2'
    if x.startswith('aFbF'):
        return 'aFbF'
    if x.startswith('aF'):
        return 'aF'
    if x.startswith('bF'):
        return 'bF'
    return None


def ng_of(x):
    """NG 担体なら射影先の NG 信号名、無ければ None。"""
    if x.startswith('LNGd'):
        return 'NGd'      # 右端が検出した会合の NG(検出遅れが +1 の代わり: 左端は待たない)
    if x == 'NG' or x.endswith('NG'):
        return 'NG'
    if 'NG' in x and (x.endswith('T3') or x.endswith('bR')):
        return 'NG'
    return None


def strip_thawed(x):
    return x[:-1] if x.endswith(THAWED) else x


def proj_left(x):
    """PL: I_{i+1} 左端が左入力(I_i 右端の生の状態)をどう読むか。
    保持(T3h)は左へ行く帰還波の話なので左端には見えない=剥がして分類する。"""
    x = strip_thawed(x)
    if x.endswith(HOLD):
        x = strip_thawed(x[:-len(HOLD)])
    if x.startswith('CB'):
        return 'CB'       # CB と帰還波の同時表示(CBT3/CBbR)も a の誕生
    if x == 'TR':
        return x
    f = fwd(x)
    if f:
        return f
    g = ng_of(x)
    if g:
        return g
    return 'L'


def proj_right(x):
    """PR: I_i 右端が右入力(I_{i+1} 左端の生の状態)をどう読むか。"""
    x = strip_thawed(x)
    if x.endswith('cBT') and x != 'cBT':
        y = x[:-3]
        if y.endswith('T3bR'):
            return 'cBTT3bR'
        if y.endswith('T3'):
            return 'cBTT3'
        if y.endswith('bR'):
            return 'cBTbR'
        return 'cBT'
    if x in ('cBT', 'cBTT3', 'cBTbR', 'cBTT3bR'):
        return x
    if x.endswith(HOLD):
        return 'W'
    if x.endswith('T3bR'):
        return 'T3bR'
    if x.endswith('T3'):
        return 'WT3'
    if x.endswith('bR'):
        return 'WbR'
    return 'W'


def add_pure(verbose=True):
    states, rules, pre, post = load(FULL_RULE)
    rm = parse_rules(rules)
    names = [state_name(s) for s in states]
    nameset = set(names)
    newn = []

    def add_state(nm, color='9090C0'):
        if nm not in nameset:
            states.append(f'{pad(nm)}@101010,{color},others')
            nameset.add(nm)
            newn.append(nm)

    b1, b2 = {}, {}
    pj = {}
    n_dense = [0]

    def put(k, t):
        """密な表への追加(既存は上書きしない)。"""
        if k not in rm:
            rm[k] = t
            n_dense[0] += 1

    transient = []   # (複合状態, 宿主): 帰還成分は次ステップに左へ去るので宿主の規則を写す

    def pickup(h, comp):
        if h == 'F':
            return None
        cand = h + comp
        if cand in nameset:
            return cand
        core = h[:-1] if h.endswith(THAWED) else h
        if core.endswith(HOLD):
            return None
        if core.endswith(comp) or (comp == 'T3' and core.endswith('T3bR')):
            return None   # 同じ帰還成分を 2 つは運べない(間隔 >= 2 で起きない)
        if h.endswith(THAWED):
            cand = core + comp + THAWED
            if cand in nameset:
                return cand
        # 新規の一時複合(例: bF2T3 = 停留中の b と通過する T3)
        add_state(cand)
        base = pj.get(h, h)
        pjc = base + comp if (base + comp) in nameset else (comp if base == 'L' and comp in nameset else base)
        pj[cand] = pjc
        transient.append((cand, h))
        return cand

    # 保持状態 = 「次状態 + T3h」: 保持中もセル自身の遷移(CB → FLbF の誕生、解凍後の
    # Balzer)は進む。(l, c, WT3) → t0+T3h(t0 = 右入力 W のときの次状態)、
    # (l', t0T3h, W) → (l', t0, WT3) の目標(無ければ W の目標 ⊕ T3)、
    # (l', t0T3h, WbR) → (l', t0, abX) の目標(自対の会合 → 解凍、~ 族へ)。

    # ── 0. 壁の即時反射(論文どおり) ─────────────────────────────────────────
    #   従来は壁セルで a/b を 1 歩表示してから反射し(aF → bFT3 → bR)、その 1 歩を
    #   故障セルの premerge(右隣の表示を同じステップに読む半径 2 の先読み)で埋めて
    #   いた。純粋シフト線では先読みできないので、壁セルは a/b の到着ステップに
    #   反射波 T3/bR を表示する(a/b とも同じだけ早まるので識別の間隔は不変)。
    n_force = [0]

    def force(k, t):
        if rm.get(k) != t:
            rm[k] = t
            n_force[0] += 1
    REFL = {'aF': 'T3', 'bF': 'bR', 'aFbF': 'T3bR'}
    wall_hosts = ('L', 'T3', 'bR', 'T3bR')
    for (l, c, r), t in list(rm.items()):
        if r == 'www' and c in wall_hosts and fwd(l) in REFL:
            force((l, c, r), REFL[fwd(l)])
    for l in list(rm.keys()):
        pass
    for lname in [x for x in names if fwd(x) in REFL]:
        for c in wall_hosts:
            force((lname, c, 'www'), REFL[fwd(lname)])

    # ── 1. 左端(B1): a 波の誕生 = CB の到着で折り返し複合を表示 ─────────────
    #   (CB, L, r) → aFcBT: a 波(aF)としてセルに乗りつつ、第 2 反射 cBT の折り返しを
    #   左行き線に見せる(線は生の状態を写す。右端は PR(aFcBT)=cBT で R2 装填)。
    #   内部の右隣は PJ(aFcBT)=aF で a 波の継続を見る。
    fold_targets = set()
    for (l, c, r), t in list(rm.items()):
        if l == 'aF' and fwd(t) == 'aF':
            b1[('CB', c, r)] = t + 'cBT'
            fold_targets.add(t)
    for x in sorted(fold_targets):
        add_state(x + 'cBT')
        pj[x + 'cBT'] = x
    # 1 セル最終区間(壁セルが左端): 折り返し cBT と即時反射 T3 を同時に表示
    for c in ('L', 'T3', 'bR', 'T3bR'):
        if ('CB', c, 'www') in rm or c == 'L':
            b1[('CB', c, 'www')] = 'cBTT3' if c in ('L', 'T3') else 'cBTT3bR'
    for l in ('bF', 'bFT3', 'bFbR'):
        force((l, 'cBTT3', 'www'), 'bR')
        force((l, 'cBTT3bR', 'www'), 'bR')
    for (l, c, r), t in list(rm.items()):
        if c in fold_targets:
            put((l, c + 'cBT', r), t)

    # ── 2. 右端(B2): 誕生の b(CB の次ステップに bF を 1 歩表示) ──────────────
    def birth(t):
        # 凍結目標 FL+rest → FLbF+rest(誕生の b を重ねる)
        if t.startswith('FL') and not t.startswith('FLbF'):
            return 'FLbF' + t[2:]
        return None
    birth_states = set()
    for (l, c, r), t in list(rm.items()):
        if c.startswith('CB') and not c.endswith(HOLD) and r in ('W', 'WT3', 'WbR', 'T3bR') and birth(t):
            b2[(l, c, r)] = birth(t)
            birth_states.add(birth(t))
    for x in sorted(birth_states):
        add_state(x)
        pj[x] = 'FL' + x[4:]
    for (l, c, r), t in list(rm.items()):
        if c.startswith('FL') and ('FLbF' + c[2:]) in birth_states:
            put((l, 'FLbF' + c[2:], r), t)

    # ── 3. 左端(B1): NG の到着 → 1 歩待ち(Lng = A3 の +1)→ 起動 Mj → 1/3 信号の誕生 ─
    #   右端の NG 担体は内部セルと同じ 1 歩通過(PL: {X}NG → NG)。左端は
    #   (NG, L+ov, r) → Lng+ov、(l, Lng+ov, r) → Mj+ov'(ov' は右隣から来る帰還成分)、
    #   (l, Mj+ov, r) → (NG, M+ov, r) の目標(= 旧 2 便目の誕生鍵: mS33 等)。
    #   Mj は右隣からは M に見える(PJ)。

    def ov_of(x):
        if x.endswith('T3bR'):
            return 'T3bR'
        if x.endswith('T3'):
            return 'T3'
        if x.endswith('bR'):
            return 'bR'
        return ''

    def with_ov(h, ov):
        return h + ov

    def lhost(ov):
        # L 宿主 ⊕ 帰還成分の既存名(L⊕T3bR は 'T3bR' と名付けられている)
        if ov == '':
            return 'L'
        return ov if ov in nameset else 'L' + ov   # 正規名は純形(T3/bR/T3bR)

    def ov_next(ov, r):
        # L 宿主に重なった帰還成分 ov が、右隣 r を見て次ステップにどうなるか
        t = rm.get(('L', lhost(ov), r))
        if t is None:
            return ov
        return ov_of(t)

    # 未起床 L 宿主の状態(帰還成分つきも: 'LT3'/'T3'、'LbR'/'bR'、'T3bR' の両名がある)
    lhost_set = {'L'} | {x for x in nameset if x in ('T3', 'bR', 'T3bR', 'LT3', 'LbR', 'LT3bR')}
    ng_hosts = sorted({c for (l, c, r) in rm if l == 'NG' and c in lhost_set})
    base_r = sorted({r for (l, c, r) in rm if c == 'L'})
    lng_states = set()
    for (l, c, r), t in list(rm.items()):
        if l == 'NG' and c in ng_hosts:
            ov = ov_of(t)          # 起動鍵の目標(MbR 等)が拾った帰還成分
            nm = 'Lng' + ov
            if nm not in nameset:
                add_state(nm)
                pj[nm] = lhost(ov)
            lng_states.add(nm)
            b1[('NG', c, r)] = nm
    mj_states = set()
    for nm in sorted(lng_states):
        ov = nm[3:]
        for r in base_r:
            if r == 'www':
                # 1 セル最終区間: NG の到着で発火する鍵 (NG, L, www)→F を 1 歩後に
                t_w = rm.get(('NG', lhost(ov), 'www'))
                if t_w is not None:
                    put(('L', nm, 'www'), t_w)
                    b1[('NG', nm, 'www')] = t_w
                continue
            ov2 = ov_next(ov, r)
            mj = 'Mj' + ov2
            if mj not in nameset:
                add_state(mj)
                pj[mj] = 'M' + ov2
            mj_states.add(mj)
            # 1 歩待ちの次は起動。2 便目の NG が来ていても消費する
            put(('L', nm, r), mj)
            b1[('NG', nm, r)] = mj
    for mj in sorted(mj_states):
        ov = mj[2:]
        for r in base_r:
            t = rm.get(('NG', 'M' + ov, r))
            if t is None:
                t0 = rm.get(('NG', 'M', r))
                if t0 is None:
                    continue
                ov2 = ov_next(ov, r)
                t = t0 + ov2 if (t0 + ov2) in nameset else t0
            put(('L', mj, r), t)
            b1[('NG', mj, r)] = t

    # ── 3b. 右端(B2): 会合セルが故障セルに落ちる対(m_i = n_i − 1、S 奇数)の会合 ──
    #   会合セル c_meet = n_i + 1 は線の先頭。規則層の奇数 S の鍵「位相 S33 のセルに
    #   R2 が到着」を、右端(位相 S33)が右入力に cBT(= 線の先頭の R2)を見た鍵で
    #   代替し、NGd(待ち無しで起動する NG)を表示する。検出が 1 セル左になる遅れが
    #   A3 の +1 停留に相当する(MEMO「(a) 純粋シフト線・A3 限定」2)。
    by_center = {}
    for (l, c, r), t in rm.items():
        by_center.setdefault(c, []).append((l, r, t))
    s33 = sorted({c for (l, c, r) in rm if r == 'W' and 'S33' in c and c[0].isupper()})
    for c in s33:
        # 中央に重なっている帰還成分(LS33T3 の T3)はこの遷移で左へ去るので持ち越さない。
        # 右入力から来る成分(cBTbR の bR)は pickup で重ねる。
        nm = 'LNGd'
        if nm not in nameset:
            add_state(nm)
            pj[nm] = 'LNG'
            for (l3, r3, t3) in by_center.get(pj[nm], []):
                put((l3, nm, r3), t3)
        for (l, r, t) in by_center.get(c, []):
            if r == 'W':
                b2[(l, c, 'cBT')] = nm
                pb = pickup(nm, 'bR')
                if pb is not None:
                    b2[(l, c, 'cBTbR')] = pb
    add_state('NGd')
    for (l, c, r), t in list(rm.items()):
        if l == 'NG' and c in ng_hosts:
            ov = ov_of(t)
            ov2 = ov_next(ov, r) if r != 'www' else ov
            mj = 'Mj' + ov2
            if mj not in nameset:
                add_state(mj)
                pj[mj] = 'M' + ov2
                mj_states.add(mj)
            b1[('NGd', c, r)] = mj if r != 'www' else t
    for mj in sorted(mj_states):
        ov = mj[2:]
        for r in base_r:
            if ('L', mj, r) in rm:
                continue
            t = rm.get(('NG', 'M' + ov, r))
            if t is None:
                t0 = rm.get(('NG', 'M', r))
                if t0 is None:
                    continue
                ov2 = ov_next(ov, r)
                t = t0 + ov2 if (t0 + ov2) in nameset else t0
            put(('L', mj, r), t)
            b1[('NG', mj, r)] = t

    # ── 4. 右端(B2): 他対の b の前進停留(bF を 2 歩表示: bF → bF2) ──────────
    #   左端は bF を無視、bF2 で b が来たとして扱う。
    add_state('bF2')
    pj['bF2'] = 'L'
    # 右端に着いた b(帰還波 T3/bR と同時の複合 bFT3/bFbR/bFT3bR も)は 1 歩停留 bF2 へ。
    # 帰還成分は次ステップに左へ去るので停留状態には残らない。
    # 境界条件 n_i + m_i = p − i では a_{i+1} が停留中の b_i に追いつく: 到着した a と
    # 停留中の b を同時に表示する aFbF2(線は aFbF として運ぶ=重ね合わせ)。
    add_state('aFbF2')
    pj['aFbF2'] = 'aF'
    for (l, c, r), t in list(rm.items()):
        if fwd(c) == 'bF' and not c.startswith('FL') and r == 'W':
            if fwd(l) == 'aF':
                b2[(l, c, 'W')] = 'aFbF2'
            elif t in ('L', 'LT3', 'LbR', 'T3', 'bR', 'T3bR', 'aF'):
                b2[(l, c, 'W')] = 'bF2'
    for (l, c, r), t in list(rm.items()):
        if c == 'L' and r == 'W':
            put((l, 'aFbF2', 'W'), t)
    # 停留明け: 保持していた b は線へ出る(表示は消える)。セルは「左入力 l を見た
    # 静止 L」と同じ遷移(a の到着 (aF,L,W)→aF、凍結起動 (C,L,W)→CB も含む)。
    for (l, c, r), t in list(rm.items()):
        if c == 'L' and r == 'W':
            put((l, 'bF2', 'W'), t)
    put(('L', 'bF2', 'W'), 'L')
    for (l, c, r), t in list(rm.items()):
        if l == 'bF':
            if ('L', c, r) in rm:
                b1[('bF', c, r)] = rm[('L', c, r)]
            put(('bF2', c, r), t)

    # ── 6. 解凍済み右端の状態族(~): 旧 FT2 レーン(右入力を壁として扱う) ──────
    #   解凍後の右端は右入力 W を見ても凍結起動せず壁として振る舞う。状態で
    #   区別するため、FT2 レーンに現れる中央状態 y を y~ に複製する。
    thawed = set()
    for (l, c, r), t in rm.items():
        if r == 'FT2':
            thawed.add(c)
            if t != 'F':
                thawed.add(t)
    # 閉包: y~ の遷移先も族に入れる(右入力 W/WT3/WbR/T3bR/cBT 系で到達するもの)
    changed = True
    while changed:
        changed = False
        for (l, c, r), t in rm.items():
            if c in thawed and r in ('FT2', 'W', 'WT3', 'WbR', 'T3bR', 'cBT') and t != 'F' and t not in thawed:
                thawed.add(t)
                changed = True
    thawed.discard('F')

    def tw(t):
        return t if (t == 'F' or t not in thawed) else t + THAWED

    for y in sorted(thawed):
        add_state(y + THAWED, color='B0A060')
        pj[y + THAWED] = y
    for (l, c, r), t in list(rm.items()):
        if c in thawed:
            if r == 'FT2':
                put((l, c + THAWED, 'W'), tw(t))
            elif r in ('WT3', 'WbR', 'T3bR', 'cBT', 'cBTT3', 'cBTbR', 'cBTT3bR'):
                put((l, c + THAWED, r), tw(t))
    # ── 5. 右端(B2): 帰還 a の停留 = T3 を 1 歩保持(cT3h)、自対なら会合で解凍 ────
    #   後処理: 右端に現れうる全ての中央状態 c((l, c, W) を持つもの)について
    #   (l, c, WT3) → cT3h、(l, cT3h, W) → (l, c, WT3) の目標(無ければ W の目標 ⊕ T3)、
    #   (l, cT3h, WbR) → (l, c, abX) の目標(b が 1 歩後ろ = 自対の会合 → 解凍)。
    # 右端の帰還 b / 第 2 反射の取り込み: (l, c, W) → t に対し
    #   (l, c, WbR) → t ⊕ bR、(l, c, cBT) → t ⊕ R2。右端専用の上書き(B2 の W 鍵)が
    #   あるときは密な表に鍵があっても上書きする(停留中の b を失わないため)。
    n_pb = 0
    for (l, c, r) in list(rm.keys()) + list(b2.keys()):
        if r != 'W' or c.endswith(HOLD):
            continue
        override = (l, c, 'W') in b2
        t = b2.get((l, c, 'W'), rm.get((l, c, 'W')))
        if not t:
            continue
        for (r_in, comp) in (('WbR', 'bR'), ('cBT', 'R2')):
            if not override and ((l, c, r_in) in rm or (l, c, r_in) in b2):
                continue
            if comp == 'R2' and (l, c, 'cBT') not in rm and not override:
                continue
            pb = pickup(t, comp)
            if pb is not None:
                b2[(l, c, r_in)] = pb
                n_pb += 1
    # ── 一時複合(宿主⊕帰還成分)の規則: 宿主の規則を写す(密/B2 とも)。保持の解放
    #    規則の生成前と最後に呼ぶ(保持状態の基底が一時複合のことがある)。
    tr_seen = set()
    tr_count = [0]

    def copy_transients():
        idx_all = {}
        for (l, c, r), t in rm.items():
            idx_all.setdefault(c, []).append((l, r, t))
        idx_b2 = {}
        for (l, c, r), t in b2.items():
            idx_b2.setdefault(c, []).append((l, r, t))
        while True:
            todo = [x for x in transient if x not in tr_seen]
            if not todo:
                break
            for (cs, h) in todo:
                tr_seen.add((cs, h))
                for (l, r, t) in list(idx_all.get(h, [])):
                    if (l, cs, r) not in rm:
                        rm[(l, cs, r)] = t
                        idx_all.setdefault(cs, []).append((l, r, t))
                        tr_count[0] += 1
                for (l, r, t) in list(idx_b2.get(h, [])):
                    if (l, cs, r) not in b2:
                        b2[(l, cs, r)] = t
                        idx_b2.setdefault(cs, []).append((l, r, t))
                        tr_count[0] += 1

    n_hold = 0
    # 右入力の基底 r_in とそれに T3 が重なった入力 r_in⊕T3(W→WT3、cBT→cBTT3、cBTbR→cBTT3bR)
    # 境界条件 n_i + m_i = p − i では b_i と a_{i+1} が同時に壁に着き、帰還対 T3bR として
    # 右端に同時に来る(T3 は保持、bR は取り込み)。
    for (r_in, r_t3) in (('W', 'WT3'), ('cBT', 'cBTT3'), ('cBTbR', 'cBTT3bR'), ('WbR', 'T3bR')):
        keys = [(l, c) for (l, c, r) in list(rm.keys()) + list(b2.keys()) if r == r_in and not c.endswith(HOLD)]
        for (l, c) in sorted(set(keys)):
            t0 = b2.get((l, c, r_in), rm.get((l, c, r_in)))
            if t0 is None or t0 == 'F' or t0.endswith(HOLD):
                continue
            hold = t0 + HOLD
            if hold not in nameset:
                add_state(hold)
                pj[hold] = pj.get(t0, t0)
            b2[(l, c, r_t3)] = hold
            n_hold += 1
    copy_transients()
    # 解放規則(保持状態ごと。t0 の W 文脈の鍵を全て写す)
    hold_states = sorted({x for x in nameset if x.endswith(HOLD)})
    idx_c = {}
    for (l, c, r), t in rm.items():
        if r == 'W':
            idx_c.setdefault(c, []).append((l, t))
    for hold in hold_states:
        t0 = hold[:-len(HOLD)]
        for (l2, t2) in idx_c.get(t0, []):
            r2 = 'W'
            t2b = b2.get((l2, t0, 'W'), t2)
            rel = pickup(t2b, 'T3')
            if rel is None:
                rel = rm.get((l2, t0, 'WT3'))
            if rel is not None:
                put((l2, hold, 'W'), rel)
            if (l2, t0, 'abX') in rm:
                put((l2, hold, 'WbR'), tw(rm[(l2, t0, 'abX')]))
            elif (l2, t0, 'WbR') in b2 or (l2, t0, 'WbR') in rm:
                put((l2, hold, 'WbR'), b2.get((l2, t0, 'WbR'), rm.get((l2, t0, 'WbR'))))
            else:
                pb = pickup(t2, 'bR')
                if pb is not None:
                    put((l2, hold, 'WbR'), pb)
            # 保持中に第 2 反射 cBT 系が来る: R2 装填と T3 の解放を同時に
            for r3 in ('cBT', 'cBTbR'):
                # 右端専用の規則(会合 LNGd 等)を優先する
                t3 = b2.get((l2, t0, r3), rm.get((l2, t0, r3)))
                if t3 is not None:
                    rel3 = pickup(t3, 'T3')
                    if rel3 is not None:
                        put((l2, hold, r3), rel3)

    # ── 6a. L 宿主の別名(LT3≡T3、LbR≡bR、LT3bR≡T3bR)の鍵を相互に補完 ──────────
    ALIAS = {k: v for k, v in {'LT3': 'T3', 'LbR': 'bR', 'LT3bR': 'T3bR'}.items()
             if k in nameset and v in nameset}
    ALIAS.update({v: k for k, v in list(ALIAS.items())})
    n_alias = 0
    for (l, c, r), t in list(rm.items()):
        if not (l in ALIAS or c in ALIAS or r in ALIAS):
            continue
        import itertools
        opts = [[x, ALIAS[x]] if x in ALIAS else [x] for x in (l, c, r)]
        for k in itertools.product(*opts):
            if k not in rm:
                rm[k] = t
                n_alias += 1

    # ── 6b. 1/3 信号の発火透過: 位相成分を剥がした基底鍵が F なら F ──────────
    #   最終区間に生まれる 1/3 信号は会合相手が無く発火時まで残る。www レーンの
    #   複製 (L, M, mS3A)→M のように発火を妨げる鍵を直す。
    def strip_phase(x):
        for ph in ('S33', 'S3A', 'S3B'):
            if ph in x:
                i = x.index(ph)
                h = x[:i]
                rest = x[i + 3:]
                if len(h) == 1 and h.islower():
                    h = h.upper()
                return h + rest
        return x
    n_fire = 0
    for (l, c, r), t in list(rm.items()):
        bl, bc, br = strip_phase(l), strip_phase(c), strip_phase(r)
        if (bl, bc, br) != (l, c, r) and rm.get((bl, bc, br)) == 'F' and t != 'F':
            rm[(l, c, r)] = 'F'
            n_fire += 1

    copy_transients()
    n_tr = tr_count[0]

    # ── 6d. 帰還対 T3bR の透過閉包 ───────────────────────────────────────────
    #   境界条件 n_i + m_i = p − i の対ごとに a_{i+1} と b_i が重なった帰還対 T3bR が
    #   壁から左走し、最終区間の起床済みセル(Balzer の状態、1/3 信号の位相、担体)を
    #   横切る。add_ab の複合 X⊕T3bR は L/A..R/aF/bF/cBT の宿主にしか無く、
    #   (rS3B, C, RT3bR) のような鍵が未定義だった(実測 n=100、p=10、境界条件 6 対)。
    #   X⊕T3 と X⊕bR が共に純透過(目標 = 基底の目標 ⊕ 成分)な鍵から X⊕T3bR 版を作る:
    #   右隣 (l, c, hT3bR) → t0T3bR(対が c に入る)、中央 (l, hT3bR, r) と左隣
    #   (hT3bR, c, r) は T3 版と bR 版の目標が一致するときその目標(対は左へ去る)。
    #   必要な状態 X⊕T3bR は基底に加える(内部セルの入力になるので 9 ビット索引内)。
    #   対象は最終区間 I_{p+1} で起こりうる鍵だけ: 凍結変状態 F(x) の宿主・目標
    #   (I_{p+1} は凍結しない)と境界専用状態(~ 族、T3h。右端は B2 で対処済み)を除く。
    #   一時複合(copy_transients)の後に行う: 右端専用の一時複合には写さない。
    n_pair = 0
    pair_states = []

    def frozen_host(x):
        return len(x) >= 2 and x[0] == 'F' and x[1] in 'ABCLMQR' and x != 'F'

    def plain(x):
        return THAWED not in x and HOLD not in x and not frozen_host(x)

    for _round in range(3):
        added = 0
        # 右隣
        for (l, c, r), t1 in list(rm.items()):
            if not r.endswith('T3') or r.endswith('T3bR'):
                continue
            h = r[:-2]
            if not (plain(l) and plain(c) and plain(h)):
                continue
            t0 = rm.get((l, c, h))
            if t0 is None or t0 == 'F' or not plain(t0) or t1 != t0 + 'T3' or rm.get((l, c, h + 'bR')) != t0 + 'bR':
                continue
            if (l, c, h + 'T3bR') in rm:
                continue
            for x in (h + 'T3bR', t0 + 'T3bR'):
                if x not in nameset:
                    add_state(x, 'B070C0')
                    pair_states.append(x)
            put((l, c, h + 'T3bR'), t0 + 'T3bR')
            added += 1
        # 中央・左隣(新設した X⊕T3bR だけ。既存の T3bR 複合の鍵は add_ab が到達分を持つ)
        pair_set = set(pair_states)
        for (l, c, r), t1 in list(rm.items()):
            if not (plain(l) and plain(c) and plain(r)):
                continue
            if c.endswith('T3') and not c.endswith('T3bR') and (c[:-2] + 'T3bR') in pair_set:
                h = c[:-2]
                if rm.get((l, h + 'bR', r)) == t1 and (l, h + 'T3bR', r) not in rm:
                    put((l, h + 'T3bR', r), t1)
                    added += 1
            if l.endswith('T3') and not l.endswith('T3bR') and (l[:-2] + 'T3bR') in pair_set:
                h = l[:-2]
                if rm.get((h + 'bR', c, r)) == t1 and (h + 'T3bR', c, r) not in rm:
                    put((h + 'T3bR', c, r), t1)
                    added += 1
        n_pair += added
        if added == 0:
            break


    # ── 7. 射影表 PL/PR ─────────────────────────────────────────────────────
    all_names = [state_name(s) for s in states]
    pl_lines = [f'PL:{x}->{proj_left(x)}' for x in all_names if proj_left(x) != x]
    pr_lines = [f'PR:{x}->{proj_right(x)}' for x in all_names if proj_right(x) != x]
    pj_lines = [f'PJ:{x}->{y}' for x, y in sorted(pj.items())]
    b1_lines = [f'B1:{pad(a)}##{pad(b)}##{pad(c)}->{pad(t)}' for (a, b, c), t in sorted(b1.items())]
    b2_lines = [f'B2:{pad(a)}##{pad(b)}##{pad(c)}->{pad(t)}' for (a, b, c), t in sorted(b2.items())]

    for x in all_names:
        assert proj_left(x) in nameset, ('PL target missing', x, proj_left(x))
        assert proj_right(x) in nameset, ('PR target missing', x, proj_right(x))
    for x, y in pj.items():
        assert y in nameset, ('PJ target missing', x, y)
    for k, t in list(b1.items()) + list(b2.items()):
        assert t in nameset and all(z in nameset for z in k), ('B state missing', k, t)
    for k, t in rm.items():
        assert t in nameset and all(z in nameset for z in k), ('dense state missing', k, t)

    # 状態の並び: 基底(元の全状態 + 射影先 NG2/NG3/bF2)を先頭に置いて 9 ビット索引
    # (512)に収め、境界専用の拡張状態はその後ろ(密な表には入らない)。拡張状態を
    # 含む規則は B0:(フラグ不問の疎な表)で書く。
    base_extra = ['bF2', 'NGd'] + pair_states
    by_name = {state_name(s): s for s in states}
    order = names + base_extra + [x for x in newn if x not in base_extra]
    assert len(order) == len(states) and len(names) + len(base_extra) <= 512
    states = [by_name[x] for x in order]
    base = set(names + base_extra)
    dense_lines, b0_lines = [], []
    for (a, b, c), t in rm.items():
        line = f'{pad(a)}##{pad(b)}##{pad(c)}->{pad(t)}'
        if a in base and b in base and c in base:
            dense_lines.append(line)
        else:
            b0_lines.append('B0:' + line)
    lines = dense_lines + b0_lines + pl_lines + pr_lines + pj_lines + b1_lines + b2_lines
    write_file(states, pre, post, lines, path=PURE_RULE)
    if verbose:
        print(f'pure: dense {len(dense_lines)}, B0 {len(b0_lines)}')
    if verbose:
        print(f'pure: wall overrides {n_force[0]}')
        print(f'pure: holds {n_hold}, bR-pickup {n_pb}, alias {n_alias}, fire-transparency {n_fire}, transient {len(transient)} ({n_tr} rules)')
        print(f'pure: T3bR pair closure {n_pair} rules, {len(pair_states)} states')
        print(f'pure: states {len(states)} (+{len(newn)}), dense rules {len(rm)} (+{n_dense[0]}), '
              f'PL {len(pl_lines)} PR {len(pr_lines)} PJ {len(pj_lines)} B1 {len(b1)} B2 {len(b2)}, '
              f'thawed family {len(thawed)}')
        print('new states:', ' '.join(newn))


def run_pure(n, spec, dump=False, trace=False):
    import subprocess
    env = dict(os.environ)
    if trace:
        env['FSSP_TRACE_FB'] = '1'
    args = [F.BIN, '-r', PURE_RULE, '-s', str(n), '-f', spec, '--a3', '--pure'] + (['-d'] if dump else [])
    p = subprocess.run(args, capture_output=True, text=True, cwd=HERE, env=env)
    return p.stdout, p.stderr


def layouts_pure(nmax2=16, nmax3=16, slack=2, last_min=2):
    """A3 配置の列挙: n_i >= m_i + slack(slack=2: 会合セルが I_i の中、=1: 境界配置
    も含む)、n_i + m_i >= p - i、最終区間 >= last_min。p=2, 3。"""
    out = []
    for n in range(5, nmax2 + 1):
        for a1 in range(2, n - 1):
            for b1 in range(a1, n - 1):
                sz = [a1 - 1, b1 - a1 + 1, n - b1]
                if sz[0] >= sz[1] + slack and sz[2] >= last_min:
                    out.append((n, f'{a1}-{b1}', sz))
    for n in range(5, nmax2 + 1):
        for a1 in range(2, n - 1):
            for b1 in range(a1, n - 1):
                for a2 in range(b1 + 2, n):
                    for b2 in range(a2, n):
                        segs = [(1, a1 - 1), (a1, b1), (b1 + 1, a2 - 1), (a2, b2), (b2 + 1, n)]
                        sz = [e - s_ + 1 for (s_, e) in segs]
                        if sz[0] < 1 or sz[-1] < last_min:
                            continue
                        if any(sz[2 * j - 2] < sz[2 * j - 1] + slack or sz[2 * j - 2] + sz[2 * j - 1] < 2 - j
                               for j in (1, 2)):
                            continue
                        out.append((n, f'{a1}-{b1},{a2}-{b2}', sz))
    for n in range(8, nmax3 + 1):
        for a1 in range(2, n - 3):
            for b1 in range(a1, n - 3):
                for a2 in range(b1 + 2, n - 1):
                    for b2 in range(a2, n - 1):
                        for a3 in range(b2 + 2, n):
                            for b3 in range(a3, n):
                                segs = [(1, a1 - 1), (a1, b1), (b1 + 1, a2 - 1), (a2, b2),
                                        (b2 + 1, a3 - 1), (a3, b3), (b3 + 1, n)]
                                sz = [e - s_ + 1 for (s_, e) in segs]
                                if sz[0] < 1 or sz[-1] < last_min:
                                    continue
                                if any(sz[2 * j - 2] < sz[2 * j - 1] + slack for j in (1, 2, 3)):
                                    continue
                                out.append((n, f'{a1}-{b1},{a2}-{b2},{a3}-{b3}', sz))
    # p=4: n_i >= m_i + slack、n_i + m_i >= p - i(i=1: >= 3)、n <= nmax4
    nmax4 = kw_nmax4[0]
    for n in range(10, nmax4 + 1):
        import itertools
        # 区間長の列 [n1,m1,n2,m2,n3,m3,n4,m4,n5] を直接列挙(各 >= 1)
        def rec(prefix, remain, k):
            if k == 9:
                if remain == 0:
                    yield prefix
                return
            lo = 1
            for v in range(lo, remain - (8 - k) + 1):
                yield from rec(prefix + [v], remain - v, k + 1)
        for sz in rec([], n, 0):
            if sz[-1] < last_min:
                continue
            if any(sz[2 * j - 2] < sz[2 * j - 1] + slack or sz[2 * j - 2] + sz[2 * j - 1] < 4 - j
                   for j in (1, 2, 3, 4)):
                continue
            pos = 1
            spec = []
            for k, v in enumerate(sz):
                if k % 2 == 1:
                    spec.append(f'{pos}-{pos + v - 1}' if v > 1 else f'{pos}')
                pos += v
            out.append((n, ','.join(spec), sz))
    return out


kw_nmax4 = [0]


def run_batch_pure(layouts):
    import re
    import subprocess
    import tempfile
    fd, path = tempfile.mkstemp(prefix='fssp_pure_', suffix='.txt')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        for (n, spec, _) in layouts:
            f.write(f'{n} {spec} a3\n')
    try:
        p = subprocess.run([F.BIN, '-r', PURE_RULE, '--pure', '--batch', path],
                           capture_output=True, text=True, cwd=HERE)
    finally:
        os.unlink(path)
    res = {}
    for line in p.stdout.splitlines():
        m = re.match(r'batch: n=(\d+) f=(\S+) a3=1 (SYNCED|NOT SYNCED) t=(\d+) injected=(\d+)', line)
        if m:
            res[(int(m.group(1)), m.group(2))] = (m.group(3) == 'SYNCED', int(m.group(4)))
    return res


def sweep_highp(seed=1, count=1500, nmax=80, pmin=5, pmax=12):
    """高 p のランダム配置スイープ(--pure --batch)。条件 n_i >= m_i+1、n_i+m_i >= p-i、
    最終 >= 1 を満たす配置を乱数で作り、2n-2+p での SYNCED を確認する。
    (p <= 4 の全数 sweep では帰還対 T3bR が最終区間の 1/3 信号と交差する配置が
    出ず、(rS3B, C, RT3bR) の未定義が n=100、p=10 まで見つからなかった)"""
    import random, collections
    rnd = random.Random(seed)

    def gen():
        p = rnd.randint(pmin, pmax)
        segs = []
        for i in range(1, p + 1):
            m = rnd.choice([1, 1, 1, 2, 2, 3, 4])
            nmin = max(m + 1, p - i - m, 1)
            segs.append((nmin + rnd.choice([0, 0, 0, 1, 2, 3]), m))
        last = rnd.randint(1, 8)
        total = sum(n + m for n, m in segs) + last
        if total > nmax:
            return None
        spec, pos = [], 1
        for n, m in segs:
            pos += n
            spec.append(f'{pos}' if m == 1 else f'{pos}-{pos + m - 1}')
            pos += m
        return (total, ','.join(spec), p)

    layouts, seen = [], set()
    while len(layouts) < count:
        g = gen()
        if g and (g[0], g[1]) not in seen:
            seen.add((g[0], g[1]))
            layouts.append(g)
    results = run_batch_pure(layouts)
    fails, byp = [], collections.Counter()
    for (n, spec, p) in layouts:
        byp[p] += 1
        synced, t = results.get((n, spec), (False, -1))
        if not synced or t != 2 * n - 2 + p:
            fails.append((n, spec, synced, t))
    print(f'high-p sweep (seed {seed}, n <= {nmax}): {len(layouts)} layouts, {len(fails)} failures; '
          f'p histogram {sorted(byp.items())}')
    for x in fails[:15]:
        print('  FAIL', x)
    return fails


def sweep_pure(slack=2, last_min=2, nmax2=16, nmax3=16, show=10, nmax4=0):
    kw_nmax4[0] = nmax4
    lay = layouts_pure(nmax2=nmax2, nmax3=nmax3, slack=slack, last_min=last_min)
    res = run_batch_pure(lay)
    fails = []
    for (n, spec, sz) in lay:
        p = (len(sz) - 1) // 2
        synced, t = res.get((n, spec), (False, -1))
        if not synced or t != 2 * n - 2 + p:
            fails.append((n, spec, sz, synced, t))
    for f in fails[:show]:
        print('FAIL n=%d -f %s sizes=%s %s t=%d' % f)
    print(f'pure sweep (n_i >= m_i+{slack}, last >= {last_min}, n<= {nmax2}/{nmax3}): '
          f'{len(lay)} layouts, {len(fails)} failures')
    return fails


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'add_pure'
    if cmd == 'add_pure':
        add_pure()
    elif cmd == 'sweep':
        sweep_pure(slack=int(sys.argv[2]) if len(sys.argv) > 2 else 2,
                   last_min=int(sys.argv[3]) if len(sys.argv) > 3 else 2)
    elif cmd == 'sweep_highp':
        sweep_highp(seed=int(sys.argv[2]) if len(sys.argv) > 2 else 1,
                    count=int(sys.argv[3]) if len(sys.argv) > 3 else 1500,
                    nmax=int(sys.argv[4]) if len(sys.argv) > 4 else 80)
    elif cmd == 'run':
        out, err = run_pure(int(sys.argv[2]), sys.argv[3], dump='-d' in sys.argv, trace=True)
        print(out)
        print(err, file=sys.stderr)
