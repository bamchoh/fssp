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
            return
        if rulemap.get(key) != target:
            if key in rulemap:
                n_override[0] += 1
            rulemap[key] = target

    def F(l, X, r):
        # 現行ルール表を choreography 遷移のオラクルとして使う
        # (add_a1/discover で合成済みの (l,X,r) キー、境界の W/www 提示を含む)
        return rulemap.get((l, X, r))

    # -- S33 誕生(細胞 1 の将軍、t=1 表示)= 論文の 1/3 放出 -------------------
    L0 = F('www', 'M', 'L')
    put(('www', 'M', 'L'), lo[L0] + 'S33')
    # -- 発火(左隣の ph2 複合が自身の仮想値を運ぶ)----------------------------
    # 内部受信: 仮想値を 1 歩進めて複合を開始
    for X in held:
        for Y in held:
            for r in held + ['W']:
                put((lo[X] + 'S3B', Y, r), lo[F(X, Y, r)] + 'S33')
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
                put((l, lo[X] + 'S33', r), lo[t1] + 'S3A')
                put((l, lo[X] + 'S3A', r), lo[t1] + 'S3B')
                put((l, lo[X] + 'S3B', r), t1)  # 復帰 = choreography の次状態
    # -- 交差(右隣が凍結表示 = 前線が滞在中に到達): 氷結複合へ切替 ------------
    # held = 発火時の仮想値(= gold[f-1])なので復帰後の F 表示とも一致する
    for X in held:
        for l in ['www'] + held:
            for r in fv:
                put((l, lo[X] + 'S33', r), X + 'S3A')
                put((l, lo[X] + 'S3A', r), X + 'S3B')
                put((l, lo[X] + 'S3B', r), 'F' + X)
    # -- 氷結複合の進行(保値一定)と復帰 --------------------------------------
    for X in held:
        for l in ['www'] + held + fv:
            for r in fv + ['W', 'CB', 'TR']:
                put((l, X + 'S33', r), X + 'S3A')
                put((l, X + 'S3A', r), X + 'S3B')
                put((l, X + 'S3B', r), 'F' + X)
    # -- 待機ホスト(左隣が ph0/ph1 を表示中): choreography は仮想値で計算 ----
    # 内部ホスト: 自身の表示 Y を 1 歩進める(左隣の仮想値 X を左入力に使う)
    for X in held:
        for Y in held:
            for r in held + ['W']:
                for ph in ('S33', 'S3A'):
                    put((lo[X] + ph, Y, r), F(X, Y, r))
        # 氷結ホスト / 氷結領域の保持(複合の左隣は自分に影響しない)
        for ph in phn:
            for v in held:
                for r in fv + ['W', 'CB', 'TR']:
                    put((lo[X] + ph, 'F' + v, r), 'F' + v)
                    put((X + ph, 'F' + v, r), 'F' + v)
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
                # S 奇数: R2 が会合セル自身に来た瞬間(細胞 c-1 は滞在 0 歩目)
                for r in fv:
                    put((v + 'S33', w + 'R2', r), w + 'NG')
    # -- NG 会合放出(1/1 右走行)と I2 左端の起動 -----------------------------
    for v in held:
        for w in held:
            for r in ['W', 'TR'] + fv:
                put((v + 'NG', 'F' + w, r), w + 'NG')   # 右走行伝播
                put((v + 'NG', 'L', 'L'), 'M')           # I2 左端の起動
                put((v + 'NG', 'L', 'www'), 'F')         # I2=1 セル区間の起動
                # 会合セル(奇数 S)の NG 2 歩表示: 細胞 c-1 の ph2 触発による
                # journey 再点火を防ぐ(ph0 を左隣に持つ NG 表示は存在しない)
                for l in held:
                    put((l + 'S3A', w + 'NG', r), w + 'NG')
    # NG の 1 歩表示後の復帰(左隣は氷結複合 ph2 のほか凍結表示/境界提示)
    for l in fheld + [h + 'S3B' for h in held]:
        for v in held:
            for r in ['W', 'TR'] + fv:
                put((l, v + 'NG', r), 'F' + v)
    ordered = emit(rulemap, list(rulemap.keys()))
    write_file(states, pre, post, ordered)
    print(f'add_meeting: states={len(states)} rules={len(ordered)} '
          f'(overrides={n_override[0]})')

