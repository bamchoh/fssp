# -*- coding: utf-8 -*-
# 未定義規則キーの検出(診断用)。-f/-d 付きでシミュレータを走らせ、各行の
# ルール入力キー (left, own, right) を再構築して、ルール表に存在しないものを
# 列挙する。discover_a1 と同じキー組み立て(stream 行の故障セルはストリーム値)。
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)
BIN = os.path.join(HERE, 'target', 'release', 'fssp.exe')
# 実行用の完全規則表(freeze_tools.add_ab の出力)で照合する
OUT_RULE = os.path.join(ROOT, 'balzer-freeze-full.rul.txt')

from freeze_tools import load, parse_rules, state_name, state_rows, stream_rows, cells, srow_cells  # noqa: E402


def check(n, spec, a3=False, verbose=True):
    states, rules, pre, post = load(OUT_RULE)
    rulemap = parse_rules(rules)
    names = [state_name(s) for s in states]

    args = [BIN, '-r', OUT_RULE, '-s', str(n), '-d', '-t', str(2 * n + 60), '-f', spec]
    if a3:
        args.append('--a3')
    p = subprocess.run(args, capture_output=True, text=True, cwd=HERE)
    text = p.stdout
    rows = state_rows(text)
    rr, ll = stream_rows(text)
    rrows = [srow_cells(r) for r in rr]
    lrows = [srow_cells(l) for l in ll]

    missing = []
    # 故障セルマスクをヘッダから取得(故障セルは遷移規則を使わない)
    mask = None
    for line in text.splitlines():
        if line.startswith('cells:'):
            mask = [False] * (n + 2)
            break
    if 'defective: ' in text:
        seg = text.split('defective: ')[1].splitlines()[0].strip()
        if seg:
            for tok in seg.split(','):
                if '-' in tok:
                    a, b = tok.split('-')
                else:
                    a = b = tok
                for cc in range(int(a), int(b) + 1):
                    mask[cc] = True
    for t in range(1, len(rows)):
        prev = cells(rows[t - 1])
        cur = cells(rows[t])
        # 発火行以降(全 F)は走査しない
        if all(x.strip() == 'F' for x in cur):
            break
        rrow = rrows[t - 1] if t - 1 < len(rrows) else None
        lrow = lrows[t - 1] if t - 1 < len(lrows) else None
        nc = len(prev)
        for k in range(nc):
            ckey = k + 1
            if mask is not None and mask[ckey]:
                continue
            own = prev[k].strip()
            if k == 0:
                l = 'www'
            elif rrow is not None and rrow[ckey - 2].startswith('['):
                l = rrow[ckey - 2][1:-1].strip()
            else:
                l = prev[k - 1].strip()
            if ckey == n:
                r = 'www'
            elif lrow is not None and lrow[k + 1].startswith('['):
                r = lrow[k + 1][1:-1].strip()
            else:
                r = prev[k + 1].strip()
            if (l, own, r) not in rulemap:
                missing.append((t, ckey, (l, own, r), cur[k].strip()))
    if verbose:
        for (t, ckey, key, got) in missing:
            print(f'  t={t} cell{ckey}: ({key[0]}, {key[1]}, {key[2]}) -> {got}  [undefined]')
        print(f'n={n} -f {spec}: {len(missing)} undefined keys used')
    return missing


if __name__ == '__main__':
    for spec in sys.argv[1:]:
        n, _, fl = spec.partition(':')
        n = int(n)
        if not fl:
            raise SystemExit('usage: python -I check_keys.py <n>:<faultspec>[,a3] ...')
        fl, _, a3 = fl.partition(',')
        check(n, fl, a3 == 'a3')