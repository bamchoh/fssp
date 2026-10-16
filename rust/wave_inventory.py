# 波インベントリ: 障害あり走行の全キャリア表示(生成/消滅/位置)の時刻表。
# 状態行の列は split('|')[i] = cell i+1(=cell1 から始まる)と実測で確定済。
#   使い方: python wave_inventory.py <n> <fault-spec>
import sys
import freeze_tools as ft


def is_carrier(v):
    # choreography 値(A..R)/凍結複合 F_v/idle 外 = 信号キャリア
    if v in ('www', ''):
        return False
    if v in ('A', 'B', 'C', 'L', 'M', 'Q', 'R'):
        return False
    if len(v) == 2 and v[0] == 'F':
        return False
    return True


def cells(t, act):
    return [c.strip() for c in act[t].split('|')]


def inventory(n, spec):
    text = ft.run(ft.BIN, ['-r', ft.OUT_RULE, '-s', str(n), '-d', '-f', spec])
    for l in text.splitlines():
        if l.startswith(('segments', 'a1:', 'SYNCED', 'NOT')):
            print('   ' + l)
    act = ft.state_rows(text)
    rows = []
    for t in range(len(act)):
        cur = cells(t, act)
        gen = [(i + 1, cur[i]) for i in range(min(n, len(cur)))
               if is_carrier(cur[i]) and (t == 0 or cells(t - 1, act)[i] != cur[i])]
        die = [(i + 1, cells(t - 1, act)[i]) for i in range(min(n, len(cells(t - 1, act))) if t else 0)
               if t and is_carrier(cells(t - 1, act)[i]) and cells(t - 1, act)[i] != cur[i]]
        if gen or die:
            rows.append((t, gen, die))
    return rows


if __name__ == '__main__':
    n = int(sys.argv[1])
    spec = sys.argv[2]
    for (t, gen, die) in inventory(n, spec):
        if gen:
            print(f't={t:3d} GEN ' + ' '.join(f'c{k}:{v}' for k, v in gen))
        if die:
            print(f'      DIE ' + ' '.join(f'c{k}:{v}' for k, v in die))