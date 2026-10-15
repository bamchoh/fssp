# -*- coding: utf-8 -*-
# 基底 choreography(golden_rows)の波構造表示(NG 局所化設計用の診断)。
import os
import sys
import io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import freeze_tools as ft

n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
last = int(sys.argv[2]) if len(sys.argv) > 2 else 10
for t in range(0, last + 1):
    rows = ft.golden_rows(n)
    if t < len(rows):
        print('t %2d' % t, '  '.join(c.strip() for c in ft.cells(rows[t])))