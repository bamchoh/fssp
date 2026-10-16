use rayon;
use rayon::prelude::IntoParallelIterator;
use rayon::prelude::ParallelIterator;
#[cfg(target_arch = "x86_64")]
use std::arch::x86_64::*;
use std::env;
use std::fs;
use std::io::{BufReader, Read};
use std::slice::from_raw_parts_mut;
use std::sync::mpsc::{channel, Receiver, Sender};
use std::time::Instant;

#[derive(Debug, Clone)]
struct State {
    name: String,
    foreground: String,
    background: String,
    class: String,
}

#[derive(Debug)]
struct Config {
    states: Vec<State>,
    general: usize,
    soldier: usize,
    firing: usize,
    external: usize,
    // 7 ビット/次元のインデックス (l<<14)+(c<<7)+r。状態数最大 128(凍結層の
    // 複合状態(凍結変状態+横断信号、S33 位相 carrier)のために 32→64 状態の
    // 5→6 ビット表現からさらに拡張。AVX2 gather パスは u16 索引のままなので
    // 64 状態以下のルールセットに限って有効)。
    rules: Box<[u8; 1 << 21]>,
}

fn dump_full(cells: &[u8], config: &Config) {
    print!("|");
    for i in 0..cells.len() {
        print!("{0: <4}|", config.states[cells[i] as usize].name);
    }
    println!();
}

fn color_code(color: &String) -> String {
    let b = i32::from_str_radix(&color[0..2].to_string(), 16).unwrap();
    let g = i32::from_str_radix(&color[2..4].to_string(), 16).unwrap();
    let r = i32::from_str_radix(&color[4..6].to_string(), 16).unwrap();

    format!("{};{};{}", r, g, b)
}

fn dumpln(cells: &[u8], config: &Config) {
    print!("|");
    for i in 1..cells.len() - 1 {
        print!(
            "\x1b[38;2;{1}m\x1b[48;2;{2}m{0: <4}\x1b[0m|",
            config.states[cells[i] as usize].name,
            color_code(&config.states[cells[i] as usize].foreground),
            color_code(&config.states[cells[i] as usize].background),
        );
    }
    println!();
}

fn dump(cells: &[u8], config: &Config) {
    print!("|");
    for i in 1..cells.len() {
        print!(
            "\x1b[38;2;{1}m\x1b[48;2;{2}m{0: <4}\x1b[0m|",
            config.states[cells[i] as usize].name,
            color_code(&config.states[cells[i] as usize].foreground),
            color_code(&config.states[cells[i] as usize].background),
        );
    }
}

fn dumpright(cells: &[u8], config: &Config) {
    for i in 0..cells.len() - 1 {
        print!(
            "\x1b[38;2;{1}m\x1b[48;2;{2}m{0: <4}\x1b[0m|",
            config.states[cells[i] as usize].name,
            color_code(&config.states[cells[i] as usize].foreground),
            color_code(&config.states[cells[i] as usize].background),
        );
    }
    println!();
}

#[derive(Debug)]
enum ParseState {
    Begin,
    FoundState(i32),
    FoundRule(i32),
}

fn calc_next(next_cells: &mut [u8], idx: usize, current: &[u8], config: &Config) {
    let mut i = idx;
    for cells in current.windows(3) {
        next_cells[i] = nextcell(cells[0], cells[1], cells[2], config);
        i += 1;
    }
}

fn nextcell(left: u8, center: u8, right: u8, config: &Config) -> u8 {
    config.rules[((left as usize) << 14) + ((center as usize) << 7) + (right as usize)]
}

fn nextline(cur: &mut [u8], nex: &mut [u8], config: &Config) {
    calc_next(nex, 1, cur, config);
}

// AVX2 gather パス。u8 表(64 状態以下のルールセットのみ。索引を u16 に圧縮した
// gathered dword の下位 1 バイトを状態として回収する。
#[cfg(target_arch = "x86_64")]
mod simd {
    use std::arch::x86_64::*;

    #[target_feature(enable = "avx2")]
    pub unsafe fn step(cur: &[u8], nex: &mut [u8], rules: &[u8]) {
        let len = cur.len();
        let zp = _mm256_setzero_si256();
        // half 内: 各 gathered dword の下位バイト(byte 0,4,8,12)を先頭 4 レーンに
        let smask = _mm256_setr_epi8(
            0, 4, 8, 12, 0, 4, 8, 12, 0, 4, 8, 12, 0, 4, 8, 12, //
            0, 4, 8, 12, 0, 4, 8, 12, 0, 4, 8, 12, 0, 4, 8, 12,
        );
        // half 内 0..3 のみ通し、4..15 をゼロ化
        let amask = _mm256_setr_epi8(
            -1, -1, -1, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, //
            -1, -1, -1, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        );

        let mut i = 1usize;
        let last = len - 1; // 中心セルの排他上限
        while i + 32 <= last {
            let base = cur.as_ptr().add(i - 1);
            let l = _mm256_loadu_si256(base as *const __m256i);
            let c = _mm256_loadu_si256(base.add(1) as *const __m256i);
            let r = _mm256_loadu_si256(base.add(2) as *const __m256i);

            // u8 → u16 拡張
            //   unpacklo: [c0..c7 | c16..c23]、unpackhi: [c8..c15 | c24..c31]
            let llo = _mm256_unpacklo_epi8(l, zp);
            let lhi = _mm256_unpackhi_epi8(l, zp);
            let clo = _mm256_unpacklo_epi8(c, zp);
            let chi = _mm256_unpackhi_epi8(c, zp);
            let rlo = _mm256_unpacklo_epi8(r, zp);
            let rhi = _mm256_unpackhi_epi8(r, zp);

            // idx16 = (l<<12) + (c<<6) + r
            let ilo = _mm256_add_epi16(
                _mm256_add_epi16(_mm256_slli_epi16(llo, 12), _mm256_slli_epi16(clo, 6)),
                rlo,
            );
            let ihi = _mm256_add_epi16(
                _mm256_add_epi16(_mm256_slli_epi16(lhi, 12), _mm256_slli_epi16(chi, 6)),
                rhi,
            );

            // u16 → u32 拡張して gathers
            // ga: c0..c7 / gb: c8..c15 / gc: c16..c23 / gd: c24..c31
            let ga = _mm256_i32gather_epi32(
                rules.as_ptr() as *const i32,
                _mm256_cvtepu16_epi32(_mm256_castsi256_si128(ilo)),
                1,
            );
            let gb = _mm256_i32gather_epi32(
                rules.as_ptr() as *const i32,
                _mm256_cvtepu16_epi32(_mm256_castsi256_si128(ihi)),
                1,
            );
            let gc = _mm256_i32gather_epi32(
                rules.as_ptr() as *const i32,
                _mm256_cvtepu16_epi32(_mm256_extracti128_si256::<1>(ilo)),
                1,
            );
            let gd = _mm256_i32gather_epi32(
                rules.as_ptr() as *const i32,
                _mm256_cvtepu16_epi32(_mm256_extracti128_si256::<1>(ihi)),
                1,
            );

            // dword 下位バイト回収:
            //   sa: [c0-3 | c4-7]  sb: [c8-11 | c12-15]
            //   sc: [c16-19 | c20-23]  sd: [c24-27 | c28-31]
            let sa = _mm256_and_si256(_mm256_shuffle_epi8(ga, smask), amask);
            let sb = _mm256_and_si256(_mm256_shuffle_epi8(gb, smask), amask);
            let sc = _mm256_and_si256(_mm256_shuffle_epi8(gc, smask), amask);
            let sd = _mm256_and_si256(_mm256_shuffle_epi8(gd, smask), amask);

            // half 内先頭 4 バイトをずらしながら OR 合成
            //   out(half0) = [c0..c15]、out(half1) = [c16..c31]
            let sa0 = _mm256_castsi256_si128(sa);
            let sa1 = _mm256_extracti128_si256::<1>(sa);
            let sb0 = _mm256_castsi256_si128(sb);
            let sb1 = _mm256_extracti128_si256::<1>(sb);
            let sc0 = _mm256_castsi256_si128(sc);
            let sc1 = _mm256_extracti128_si256::<1>(sc);
            let sd0 = _mm256_castsi256_si128(sd);
            let sd1 = _mm256_extracti128_si256::<1>(sd);

            let outlo = _mm_or_si128(
                _mm_or_si128(sa0, _mm_bslli_si128::<4>(sa1)),
                _mm_or_si128(_mm_bslli_si128::<8>(sb0), _mm_bslli_si128::<12>(sb1)),
            );
            let outhi = _mm_or_si128(
                _mm_or_si128(sc0, _mm_bslli_si128::<4>(sc1)),
                _mm_or_si128(_mm_bslli_si128::<8>(sd0), _mm_bslli_si128::<12>(sd1)),
            );
            let out = _mm256_set_m128i(outhi, outlo);

            _mm256_storeu_si256(nex.as_mut_ptr().add(i) as *mut __m256i, out);
            i += 32;
        }
        // 端数はスカラーで
        while i < last {
            nex[i] = rules[((cur[i - 1] as usize) << 14)
                + ((cur[i] as usize) << 7)
                + (cur[i + 1] as usize)];
            i += 1;
        }
    }
}

fn nextline_par(ary_pair: &mut AryPair, config: &Config, use_avx2: bool) {
    #[cfg(target_arch = "x86_64")]
    {
        if use_avx2 && config.states.len() <= 64 {
            unsafe { simd::step(ary_pair.cur, ary_pair.nex, &config.rules[..]) };
            return;
        }
    }
    let _ = use_avx2;
    calc_next(ary_pair.nex, 1, ary_pair.cur, config);
}

fn per_nextline(current: &[u8], next_cells: &mut [u8], config: &Config) {
    let mid_point = current.len() / 2;
    let (first, second) = next_cells.split_at_mut(mid_point);
    rayon::join(
        || {
            calc_next(first, 1, &current[..(mid_point + 1)], config);
        },
        || {
            calc_next(second, 0, &current[(mid_point - 1)..], config);
        },
    );
}

fn parse_begin(line: &str) -> ParseState {
    if line.starts_with("state_number") {
        let v: Vec<&str> = line.split(" ").collect();
        let i: i32 = v[1].parse().unwrap();
        return ParseState::FoundState(i);
    } else if line.starts_with("rule_number") {
        let v: Vec<&str> = line.split(" ").collect();
        let i: i32 = v[1].parse().unwrap();
        return ParseState::FoundRule(i);
    }
    ParseState::Begin
}

fn parse_states(line: &str, n: i32) -> (ParseState, State) {
    let v: Vec<&str> = line.split(&['@', ','][..]).collect();

    let state = State {
        name: v[0].to_string(),
        foreground: v[1].to_string(),
        background: v[2].to_string(),
        class: v[3].to_string(),
    };

    if n == 1 {
        (ParseState::Begin, state)
    } else {
        (ParseState::FoundState(n - 1), state)
    }
}

fn parse_rules(line: &str, n: i32, states: &Vec<State>) -> (ParseState, usize, usize) {
    let v: Vec<&str> = line.split("->").collect();

    let vv: Vec<&str> = v[0].split("##").collect();

    let mut idx: usize = 0;

    for rule_state in vv {
        for (i, state) in states.iter().enumerate() {
            if rule_state == state.name.as_str() {
                idx = (idx << 7) + i;
                break;
            }
        }
    }

    let mut n_idx: usize = 0;
    for (i, state) in states.iter().enumerate() {
        if v[1] == state.name.as_str() {
            n_idx = i;
            break;
        }
    }

    if n == 1 {
        (ParseState::Begin, idx, n_idx)
    } else {
        (ParseState::FoundRule(n - 1), idx, n_idx)
    }
}

fn parse_rule_file(s: &String) -> Config {
    let mut parse_state = ParseState::Begin;
    let mut config = Config {
        states: vec![],
        rules: vec![0u8; 1 << 21].into_boxed_slice().try_into().unwrap(),
        general: 0,
        soldier: 0,
        firing: 0,
        external: 0,
    };

    for line in s.lines() {
        parse_state = match parse_state {
            ParseState::Begin => parse_begin(&line),
            ParseState::FoundState(n) => {
                let (next_state, state) = parse_states(&line, n);
                config.states.push(state);
                next_state
            }
            ParseState::FoundRule(n) => {
                let (next_state, i, j) = parse_rules(&line, n, &config.states);

                config.rules[i] = j as u8;

                next_state
            }
        };
    }

    config
}

fn read_file(path: String) -> Result<String, String> {
    let mut file_content = String::new();

    let mut fr = fs::File::open(path)
        .map(|f| BufReader::new(f))
        .map_err(|e| e.to_string())?;

    fr.read_to_string(&mut file_content)
        .map_err(|e| e.to_string())?;

    Ok(file_content)
}

fn fired(cells: &[u8], firing: usize) -> bool {
    for i in 1..cells.len() - 1 {
        if cells[i] as usize != firing {
            return false;
        }
    }
    true
}

fn new_line(n: usize, config: &Config) -> Vec<u8> {
    let mut cells: Vec<u8> = vec![config.soldier as u8; n + 2];
    cells[0] = config.external as u8;
    cells[n + 1] = config.external as u8;
    cells
}

fn first_line(n: usize, config: &Config) -> Vec<u8> {
    let mut cells = new_line(n, config);
    cells[1] = config.general as u8;
    cells
}

// ruby の Simulator#dump と同じ書式(状態名を | でつなぐ、色なし)。
// -d モードで ruby と diff 比較できるようにするため。
fn dump_plain(cells: &[u8], config: &Config) {
    let names: Vec<&str> = cells[1..cells.len() - 1]
        .iter()
        .map(|&c| config.states[c as usize].name.as_str())
        .collect();
    println!("{}", names.join("|"));
}

// dump 行を書き出す。color なら dumpln(ルールファイルの前/背景色を
// 使った ANSI 出力、1〜発火ステップまでの実セルのみ)を用いる。
fn dump_line(cells: &[u8], config: &Config, color: bool) {
    if color {
        dumpln(cells, config);
        return;
    }
    dump_plain(cells, config);
}

// -d モード用: 逐次で 1 ステップごとに途中経過を出力する。
// 出力は「初期配置 + 1〜発火ステップまでの各行」で ruby -d と同じ列になる。
// (並列ではワーカー間のトークン同期の中で全体を一つのタイミングで
//  参照できる地点がないため、ダンプモードは逐次実行にしている)
fn simulate_dump(
    current: &mut Vec<u8>,
    next_cells: &mut Vec<u8>,
    config: &Config,
    n: usize,
    color: bool,
) -> usize {
    dump_line(current, config, color);
    let mut t = 0;
    while !fired(current, config.firing) && t <= ((n << 1) - 2) {
        nextline(current, next_cells, config);
        t += 1;
        std::mem::swap(current, next_cells);
        dump_line(current, config, color);
    }
    t
}

fn simulate<'a>(
    mut current: &'a mut [u8],
    mut next_cells: &'a mut [u8],
    config: &'a Config,
    n: usize,
) -> usize {
    let mut t = 0;
    while !(fired(current, config.firing) || (t > ((n << 1) - 2))) {
        nextline(current, next_cells, config);
        t += 1;
        (current, next_cells) = (next_cells, current);

        #[cfg(debug_assertions)]
        dumpln(current, config);
    }
    t
}

struct AryPair<'a> {
    cur: &'a mut [u8],
    nex: &'a mut [u8],
    sender: [Option<Sender<i32>>; 2],
    receiver: [Option<Receiver<i32>>; 2],
}

fn split<'a>(ary1: &'a mut [u8], ary2: &'a mut [u8], n: isize) -> Vec<AryPair<'a>> {
    let ary_len = ary1.len() as isize;
    let real_len = ary_len - 2;

    // 実セル(1..real_len)を各スレッドに割り当てる。
    // slice は「左隣セル + 実セル群 + 右隣セル」で構成するため、
    // 必ず slice 長 >= 3(interior に実セルが >= 1)になるようにする。
    let segments = if n > real_len { real_len } else { n };
    let chunk = ((real_len as f64 / segments as f64).ceil() as isize).max(1);

    let mut idx_ofs = 1; // 未割り当ての実セルの先頭
    let mut splitted_ary: Vec<AryPair> = vec![];

    while idx_ofs <= real_len {
        let mut cell_end = idx_ofs + chunk;
        if cell_end > real_len + 1 {
            cell_end = real_len + 1;
        }

        let start_idx = idx_ofs - 1; // 左隣セル(先頭スライスは外部セル側の 0 から)
        let end_idx = cell_end + 1; // 右隣セル

        let ptr1 = unsafe { ary1.as_mut_ptr().offset(start_idx) };
        let ptr2 = unsafe { ary2.as_mut_ptr().offset(start_idx) };

        let size = end_idx - start_idx;

        let sp_ary1 = unsafe { from_raw_parts_mut(ptr1, size as usize) };
        let sp_ary2 = unsafe { from_raw_parts_mut(ptr2, size as usize) };

        splitted_ary.push(AryPair {
            cur: sp_ary1,
            nex: sp_ary2,
            sender: [None, None],
            receiver: [None, None],
        });

        idx_ofs = cell_end;
    }

    for i in 1..splitted_ary.len() {
        let (lsend, rrecv) = channel::<i32>();
        let (rsend, lrecv) = channel::<i32>();

        splitted_ary[i - 1].sender[1] = Some(lsend);
        splitted_ary[i].receiver[0] = Some(rrecv);
        splitted_ary[i].sender[0] = Some(rsend);
        splitted_ary[i - 1].receiver[1] = Some(lrecv);
    }

    splitted_ary
}

fn par_simulate<'a>(
    cur: &'a mut [u8],
    nex: &'a mut [u8],
    config: &'a Config,
    n: usize,
    use_avx2: bool,
) {
    let ary;
    if n > 100 {
        ary = split(cur, nex, rayon::current_num_threads() as isize);
    } else {
        ary = split(cur, nex, 2);
    }

    // 一斉射撃の最適ステップ数。正しい解は 2n-2 が下限で必ずここで全セルが
    // 発火するので、全セグメントを同じ回数だけ回し、判定は最後に全体で 1 回行う。
    // セグメント単位で fired() を見て早期離脱させると、先に自分の範囲が
    // 全発火した側がトークン交換をやめてしまい、隣が recv で詰まって
    // RecvError で panic する(同期しないルールセットで発生)。
    // 2n-2 は常に偶数なので、swap 回数が偶数に戻り最終状態は cur 側に載る。
    let steps = (n << 1) - 2;

    ary.into_par_iter().for_each(|mut ary_pair| {
        for _ in 0..steps {
            nextline_par(&mut ary_pair, config, use_avx2);

            for sender in &ary_pair.sender {
                if let Some(ref s) = sender {
                    s.send(0).unwrap();
                };
            }

            for receiver in &ary_pair.receiver {
                if let Some(ref r) = receiver {
                    r.recv().unwrap();
                };
            }

            (ary_pair.cur, ary_pair.nex) = (ary_pair.nex, ary_pair.cur);
        }
    });
}

// 故障区間。1 始まり inclusive (start, end)、昇順・非重複・隣接を統合済み。
// (Fault-Tolerant FSSP SPEC.md / Umeo 2004 §2.3.1 の defective segment に対応)
struct FaultSpec {
    segments: Vec<(usize, usize)>,
}

// 配列の区間構造。intact=true が正常区間 I_i、false が故障区間 D_i。
#[derive(Debug)]
struct FaultSeg {
    intact: bool,
    start: usize,
    end: usize,
}

// 分析結果。mask はセル配列 (0..=n+1、境界セル込み) と同じインデックスで
// 並行する故障フラグ。故障挙動の実装(次ステップ)でそのまま引き渡す。
#[derive(Debug)]
struct FaultAnalysis {
    mask: Vec<bool>,
    segments: Vec<FaultSeg>,
}

fn parse_fault_spec(spec: &str, n: usize) -> Result<FaultSpec, String> {
    if spec.trim().is_empty() {
        return Err("empty fault spec".to_string());
    }

    let mut tokens: Vec<(usize, usize)> = vec![];
    for token in spec.split(',') {
        let token = token.trim();
        let (left, right) = match token.split_once('-') {
            Some((l, r)) => (l, r),
            None => (token, token),
        };
        let (a, b) = match (left.parse::<usize>(), right.parse::<usize>()) {
            (Ok(a), Ok(b)) => (a, b),
            _ => return Err(format!("invalid fault segment: {}", token)),
        };
        if a == 0 || b > n {
            return Err(format!("cell position out of range 1..{}: {}", n, token));
        }
        if a > b {
            return Err(format!("invalid fault segment (end < start): {}", token));
        }
        tokens.push((a, b));
    }

    tokens.sort_unstable();

    for pair in tokens.windows(2) {
        if pair[1].0 <= pair[0].1 {
            return Err(format!(
                "overlapping fault segments: {}-{} and {}-{}",
                pair[0].0, pair[0].1, pair[1].0, pair[1].1
            ));
        }
    }

    // 隣接(接触)する区間は同じ故障区間なので統合する。-f 3,4 は -f 3-4 と等価。
    // 統合しないと間に空の正常区間が挟まり I_i / D_i の構造分析が壊れる。
    let mut segments: Vec<(usize, usize)> = vec![];
    for &(a, b) in &tokens {
        match segments.last_mut() {
            // 直前の区間と接触していれば 1 つの故障区間に統合する
            Some(last) if last.1 + 1 >= a => last.1 = last.1.max(b),
            _ => segments.push((a, b)),
        }
    }

    // 論文 §2.3.1: 配列は両端に正常区間を持つ配列に限って調べられる(一般性を
    // 失わない制限)。将軍は左端のセル 1 なので、セル 1 と n は正常でなければならない。
    let first = segments.first().unwrap();
    let last = segments.last().unwrap();
    if first.0 == 1 {
        return Err("cell 1 (general) must be intact".to_string());
    }
    if last.1 == n {
        return Err(format!("cell {} (right end) must be intact", n));
    }

    Ok(FaultSpec { segments })
}

fn analyze_faults(fault: &FaultSpec, n: usize) -> FaultAnalysis {
    let mut mask = vec![false; n + 2];
    for &(a, b) in &fault.segments {
        for cell in &mut mask[a..=b] {
            *cell = true;
        }
    }

    // I1 D1 I2 D2 ... Ip Dp I(p+1) の交互構造に展開する
    let mut segments: Vec<FaultSeg> = vec![];
    let mut pos = 1;
    for &(a, b) in &fault.segments {
        segments.push(FaultSeg {
            intact: true,
            start: pos,
            end: a - 1,
        });
        segments.push(FaultSeg {
            intact: false,
            start: a,
            end: b,
        });
        pos = b + 1;
    }
    segments.push(FaultSeg {
        intact: true,
        start: pos,
        end: n,
    });

    FaultAnalysis { mask, segments }
}

// 条件判定とサマリー出力。判定値を (A1/A2 の失敗リスト, A3 の失敗リスト) で返し、
// --strict の判定は main 側で行う。
fn report_faults(analysis: &FaultAnalysis) -> (Vec<String>, Vec<String>) {
    // 正常区間 I_i と故障区間 D_i の数。構造は必ず D と I が交互で I が 1 多い。
    let p = analysis.segments.iter().filter(|s| !s.intact).count();

    let mut fail_a1a2: Vec<String> = vec![];
    let mut fail_a3: Vec<String> = vec![];

    // D_i は交互構造の (2i) 番目(1 始まり)。左隣の正常区間が I_i。
    for i in 1..=p {
        let d = &analysis.segments[i * 2 - 1];
        let intact = &analysis.segments[i * 2 - 2];
        let n_i = intact.end - intact.start + 1;
        let m_i = d.end - d.start + 1;

        // A1/A2 の前提 (Theorem 2/3): n_i >= m_i
        if n_i < m_i {
            fail_a1a2.push(format!(
                "D{} (I{}={} < D{}={})",
                i, i, n_i, i, m_i
            ));
        }
        // A3 の追加条件 (Theorem 4): n_i + m_i >= p - i (i = 1..p)
        let need = p - i;
        if n_i + m_i < need {
            fail_a3.push(format!(
                "D{} (n_i+m_i={} < p-i={})",
                i,
                n_i + m_i,
                need
            ));
        }
    }

    let mut counts = [0usize; 2]; // [defective, intact] の出現順カウンタ
    let seg_str: Vec<String> = analysis
        .segments
        .iter()
        .map(|s| {
            counts[s.intact as usize] += 1;
            format!(
                "{}{}[{}-{}]",
                if s.intact { "I" } else { "D" },
                counts[s.intact as usize],
                s.start,
                s.end
            )
        })
        .collect();
    println!("segments: {}", seg_str.join(" "));

    if fail_a1a2.is_empty() {
        println!("constraint n_i>=m_i (A1/A2): OK");
    } else {
        println!("constraint n_i>=m_i (A1/A2): FAIL ({})", fail_a1a2.join(", "));
    }
    if fail_a3.is_empty() {
        println!("constraint n_i+m_i>=p-i (A3): OK");
    } else {
        println!(
            "constraint n_i+m_i>=p-i (A3): FAIL ({})",
            fail_a3.join(", ")
        );
    }
    println!("note: defective cells relay signals at 1/1 (two one-way pipelines); intact segments sync per Umeo A1/A2");

    (fail_a1a2, fail_a3)
}

// ── 故障セル中継層 + 制御層 ──────────────────────────────────────────────
// 故障セルのモデル:
//   - 故障セルは遷移規則を使わず、双方向の 1/1 パイプライン(§2.3.2, Fig.5)。
//     左から来た値はそのまま右へ、右から来た値はそのまま左へ(方向保存)。
//   - 隣接セルの故障/正常は故障フラグ(mask)で判定する。www は配列の外側の壁のみ。
// 信号層(A1 モード = -f 指定かつ --inject/--wallset なし):
//   - 管路は規則層で実装した凍結波/解凍波(balzer-freeze.rul.txt の F 状態)への
//     起動信号だけを運ぶ。信号は A1/A2 の式(t_i, t_i1, Δt_i)から区間構造より
//     事前計算し、管路の遠位口から注入して 1/1 で流れ、近位口のルール入力に
//     ちょうど 1 ステップだけ提示される(壁の役割と同じ提示機構)。
//   - 管路が信号を運んでいないときは www を提示する(区間の境界は、区間内の
//     同期にとって外側の壁と同じ振る舞いをする)。
//   - 凍結そのもの、解凍の波、区間内の同期はすべて遷移規則で動く。
// オラクルモード(--inject/--wallset あり):
//   - ctrl チャネルによる凍結/解凍注入と、素の状態中継(以前の実験機構)。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Ctrl {
    None,
    Freeze,
    Thaw,
}

// A1/A2 のスケジュールに従い管路へ注入される信号。
// time = 管路の流入口セルに値が現れる時刻、cell = 流入口セル(故障セル)。
// to_left = true なら R→L 管路(to_left、右口から注入)、false なら L→R(to_right、
// 左口から注入)。信号は管路を 1/1 で流れ、近位口で 1 ステップだけ提示されて消える。
struct PlannedSignal {
    time: usize,
    cell: usize,
    to_left: bool,
    value: u8,
}

fn find_state_index(config: &Config, name: &str) -> Option<u8> {
    config
        .states
        .iter()
        .position(|s| s.name.trim() == name)
        .map(|i| i as u8)
}

// Umeo §3 (A1/Theorem 2, A2/Theorem 3) の式から信号スケジュールを組み立てる。
//  正常区間 I_i (i = 1..p+1) の将軍起動:  t_i  = 2 Σ_{j<i} (n_j + m_j)
//  I_i の凍結起動:     t_i1 = t_i + n_i - 1 (i ≤ p、A1 本文の t1 = n1 - 1 と一致)
//  I_i の凍結期間:     Δt_i = 2 m_i + 2 Σ_{j=i+1..p} (n_j + m_j) + 2 n_{p+1}
//  I_i の解凍起動:     t_i2 = t_i1 + Δt_i
// 信号は対応する故障区間 D の管路に流し込まれ、提示時刻(起動時刻の 1 ステップ前)
// に近位口のルール入力へ現れる。
fn plan_a1_signals(analysis: &FaultAnalysis, config: &Config, a3: bool) -> (Vec<PlannedSignal>, Vec<String>) {
    let mut plan = vec![];
    let mut notes = vec![];
    let p = analysis.segments.iter().filter(|s| !s.intact).count();
    let fs = find_state_index(config, "FS");
    let ft = find_state_index(config, "FT");
    let ng = find_state_index(config, "NG");

    if fs.is_none() || ft.is_none() || ng.is_none() {
        notes.push(
            "rule file lacks FS/FT/NG signal states; A1 scheduling skipped (segments run undelayed)"
                .to_string(),
        );
        return (vec![], notes);
    }

    let sizes: Vec<usize> = analysis
        .segments
        .iter()
        .map(|s| s.end - s.start + 1)
        .collect();
    let n_last = sizes[sizes.len() - 1];

    // I_i (1-based) = segments[2(i-1)]。t_i = 2 Σ_{j<i} (n_j + m_j)。
    // A3 (Theorem 4): 起動は区間ごとに 1 ステップ遅れる (+ (i-1))。
    let t_i = |i: usize| -> usize {
        2 * analysis
            .segments
            .iter()
            .take(2 * (i - 1))
            .map(|s| s.end - s.start + 1)
            .sum::<usize>()
            + if a3 { i - 1 } else { 0 }
    };

    for i in 1..=p {
        let d = &analysis.segments[2 * i - 1]; // D_i
        let m_i = d.end - d.start + 1;
        let n_i = analysis.segments[2 * i - 2].end - analysis.segments[2 * i - 2].start + 1;
        // Δt_i = 2 m_i + 2 Σ_{j=i+1..p} (n_j + m_j) + 2 n_{p+1}
        // (Σ は I_i より後の I_{p+1} を除く全区間の合計。論文 Thm.3 の式)
        // A3 (Thm.4): 解凍信号の各区間横断の待ち分 (p - i + 1) をさらに加える。
        let delta = 2 * m_i
            + 2 * analysis
                .segments
                .iter()
                .take(2 * p)
                .skip(2 * i)
                .map(|s| s.end - s.start + 1)
                .sum::<usize>()
            + 2 * n_last
            + if a3 { p - i + 1 } else { 0 };
        // 解凍信号 FT: p=1 では注入しない(壁反射の T3 波の遠位口 pickup が
        // 純 FT に変換して同じ時刻に提示する=FT 注入を撤廃した p=1 経路)。
        // p>=2 では区間識別つき往復波が未実装のため、各管路への隔離配信(注入)
        // で解凍提示する(論文に対する既知の置換装置の記録: SPEC「実装の記録」)。
        let t_1i = t_i(i) + n_i - 1;
        let t_2i = t_1i + delta;
        let local_thaw = p == 1 && n_i >= 2;
        if let Some(ft) = ft {
            if n_i >= 2 {
                notes.push(format!(
                    "I{} frozen Δt={} (freeze via boundary reflection at t={}, FT at t={})",
                    i,
                    delta,
                    t_1i,
                    t_2i - 1
                ));
            } else {
                notes.push(format!(
                    "I{} frozen Δt={} (FS injected at t={}, FT at t={})",
                    i,
                    delta,
                    if t_1i == 0 { 0 } else { t_1i - 1 },
                    t_2i - 1
                ));
            }
            // n_i = 1 の縮退区間だけ FS を残す(C 波が存在しない: i=1 では t_1i =
            // 0 をクランプ、i>=2 では t_1i = t_i なので通常の 1 ステップ前)。
            if n_i == 1 {
                if let Some(fs) = fs {
                    let fs_time = if t_1i == 0 { 0 } else { t_1i - 1 };
                    plan.push(PlannedSignal {
                        time: fs_time,
                        cell: d.start,
                        to_left: true,
                        value: fs,
                    });
                }
            }
            if !local_thaw {
                plan.push(PlannedSignal {
                    time: t_2i - 1,
                    cell: d.start,
                    to_left: true,
                    value: ft,
                });
            }
        }
        // 次の正常区間 I_{i+1} の将軍起動信号(管路の L→R パイプラインを流れて
        // I_{i+1} の左端セルの左入力に提示される)。p=1 では全配置が会合機械
        // (CA 局所)で扱えるとは限らない(dt <= S では放出波が解凍前線に吸収
        // されるので注入での代替を残す=後段課題)。
        // p>=2 でも I_i 対 D_i 対 I_{i+1} は p=1 と同じ対面幾何なので、
        // 同じ会合機械(規則層の規則は時刻に依存しない=そのまま再利用)で
        // 処理できる。配信可能条件は p=1 のもののペア版:
        //   - 会合セル c_m <= n_i(管路への装填は S3A+TR 同時放出キーで c=n1 でも可)
        //   - dt > S_i で起動波が解凍前線に吸収されない
        //   - n_{i+1} != 1(n_{i+1}=1 では壁反射が R2 装填のポート提示を奪う)
        if let Some(ng) = ng {
            let s1 = n_i + m_i;
            let c_meet = if s1 % 2 == 0 { s1 / 2 + 1 } else { (s1 + 3) / 2 };
            let n_next = sizes[2 * i];
            // A3 (Thm.4) は時刻系に +(i−1) のオフセットを足すため会合放出の
            // 配信時刻が 1 ステップ早くなる。会合機械は A1/A2 の時刻系なの
            // で、A3 モードでは注入のまま(残課題: A3 対応の放出キー)。
            // to_left 遠位口の TR pickup は D1 だけ有効(残課題②-A2 で per-pipe 化
            // するまでは i>=2 の対は会合機械が走らないので注入を残す)。
            let pair_deliverable = !a3
                && i == 1
                && c_meet <= n_i
                && 2 * m_i + 2 * n_next > s1
                && n_next != 1;
            let s1_last = sizes[sizes.len() - 1];
            let deliverable = 2 * m_i + 2 * s1_last > s1;
            if p == 1 {
                if c_meet > n_i || !deliverable || s1_last == 1 {
                    plan.push(PlannedSignal {
                        time: t_i(i + 1) - 1 - (m_i - 1),
                        cell: d.start,
                        to_left: false,
                        value: ng,
                    });
                }
            } else if !pair_deliverable {
                plan.push(PlannedSignal {
                    time: t_i(i + 1) - 1 - (m_i - 1),
                    cell: d.start,
                    to_left: false,
                    value: ng,
                });
            }
            notes.push(format!("I{} general launched at t={}", i + 1, t_i(i + 1)));
        }
    }
    (plan, notes)
}

struct FaultField {
    mask: Vec<bool>,      // 実セル 1..=n が true。0 と n+1(壁)は false
    to_right: Vec<u8>,    // L→R ストリーム: c が t に保持する「左に由来する値」
    to_left: Vec<u8>,     // R→L ストリーム: c が t に保持する「右に由来する値」
    ctrl: Vec<Ctrl>,      // 制御メッセージチャネル(1..=n)
    frozen: Vec<bool>,    // 凍結フラグ(正常セルのみ効果あり)
    to_right_nex: Vec<u8>,
    to_left_nex: Vec<u8>,
    ctrl_nex: Vec<Ctrl>,
    // A1 モード: 管路は信号だけを運び、アイドル時は境界マーカーを提示する
    signal_only: bool,
    // 信号状態の番号。ルールファイルに無ければ None(www にフォールバック)
    w_state: Option<u8>,  // R→L 管路のアイドル提示(解凍前の境界マーカー)
    ft_state: Option<u8>, // 解凍信号
    ft2_state: Option<u8>, // 解凍後のアイドル提示(σ 線上の壁反射と凍結起動の区別)
    cb_state: Option<u8>,  // 境界 Carrier(fast 信号の管路 entry pickup 対象)
    tr_state: Option<u8>,  // fast 継続の対岸横断状態
    a3: bool,              // A3 時刻系(会合機械の規則を凍結する)
    // 壁反射の左走行波 T3 と、起床済みセル上の複合 carrier <X>T3(chateau+signal)。
    // 遠位口 pickup の対象。pickup した複合は純 FT に変換して管路に乗せる。
    t3_carriers: Vec<u8>,
    // NG 会合放出の保値付き carrier {X}NG(名称が 3 文字で NG で終わる)。
    // 会合セルから 1/1 右走行して I1 右端に現れたとき、to_right 入口 pickup
    // (入口セル d1.0 = D1 左端だけ)がこれを管路に乗せる。
    ng_carriers: Vec<u8>,
    // 故障区間数。to_left 管路への FT 変換 pickup は p=1 のときだけ有効
    // (p>=2 では各区間の解凍は注入 FT で提示され、壁反射波を区間に拾わせると
    //  他区間の波が誤って解凍を起動するため、波は管路の遠位口で消える)
    p: usize,
    // D1 の(入口セル, 遠位口セル)。CB の to_right pickup は入口セル d1.0 だけ、
    // to_left の変換 pickup は p=1 のとき d1.1 で有効。
    // None = 旧挙動(全管路で pickup、テスト用)。
    d1: Option<(usize, usize)>,
}

impl FaultField {
    fn intact(&self, c: usize) -> bool {
        !self.mask[c]
    }

    // 対岸の正常セルの表示 d が管路に乗せる解凍信号(FT)かどうか。
    // 純 FT に加え、壁反射の左走行波 T3 と複合 carrier <X>T3 も対象:
    // 複合は chateau 部分をセル自身に残すので、管路には純 FT を載せる。
    // TR は I2 左端の fast 継続表示: R2 装填(会合の 1/1 左走行反射信号)の
    // 源流として素の TR 状態のまま to_left 管路に乗せる(近位口のルール入力に
    // 1 歩だけ提示され、I1 右端の (F_l, F_v, TR) -> {v}R2 で凍結領域を左走行)。
    fn pickup_thaw(&self, d: u8) -> Option<u8> {
        if Some(d) == self.ft_state || self.t3_carriers.contains(&d) {
            self.ft_state
        } else if Some(d) == self.tr_state {
            Some(d)
        } else {
            None
        }
    }

    fn new(mask: &[bool], config: &Config, signal_only: bool) -> FaultField {
        Self::new_ctx(mask, config, signal_only, 0, None, false)
    }

    fn new_ctx(
        mask: &[bool],
        config: &Config,
        signal_only: bool,
        p: usize,
        d1: Option<(usize, usize)>,
        a3: bool,
    ) -> FaultField {
        let n = mask.len() - 2;
        let w_state = find_state_index(config, "W");
        let ft_state = find_state_index(config, "FT");
        let ft2_state = find_state_index(config, "FT2");
        let cb_state = find_state_index(config, "CB");
        let tr_state = find_state_index(config, "TR");
        // T3 と複合 carrier <X>T3(X ∈ A,B,C,L,M,Q,R)の状態番号。名前の
        // 規約は「T3 で終わる 2〜3 文字の名前」。ルールファイルに無ければ空。
        let t3_carriers: Vec<u8> = config
            .states
            .iter()
            .enumerate()
            .filter(|(_, s)| {
                let name = s.name.trim();
                name == "T3" || (name.len() == 3 && name.ends_with("T3"))
            })
            .map(|(i, _)| i as u8)
            .collect();
        // NG 会合放出の保値付き carrier {X}NG(3 文字で NG で終わる名前)。
        let ng_carriers: Vec<u8> = config
            .states
            .iter()
            .enumerate()
            .filter(|(_, s)| {
                let name = s.name.trim();
                name.len() == 3 && name.ends_with("NG")
            })
            .map(|(i, _)| i as u8)
            .collect();
        // 信号層のアイドル値。R→L 管路(I_i 右端セルの右入力)は境界マーカー W
        // を提示する(www だと凍結起動と素の壁反射 (C,L,www)->M が区別できず衝突)。
        // 解凍信号 FT が口を通過した後は FT2(解凍後位相)に切り替わる。
        // L→R 管路(次の区間 I_{i+1} 左端セルの左入力)は www(将軍の boundary
        // 規則 (www,M,*) がそのまま働く)。ルールファイルに W がなければ www。
        // L→R 管路のアイドル提示: 素の www(将軍の boundary 規則 (www,M,*) が
        // そのまま働く=以前の挙動)から L に変更。S33 排出 (www,M,L)->複合 が
        // I_{i+1} 左端の発射後 M 表示でも誤発火するため、管路からの左入力は
        // 細胞 1 と区別できる L を提示する((L,M,r) ミラー規則を生成する)。
        let idle_to_right = if signal_only { config.soldier as u8 } else { config.external as u8 };
        let idle_to_left = if signal_only { w_state.unwrap_or(config.external as u8) } else { config.soldier as u8 };
        let mut field = FaultField {
            mask: mask.to_vec(),
            to_right: vec![idle_to_right; n + 2],
            to_left: vec![idle_to_left; n + 2],
            ctrl: vec![Ctrl::None; n + 2],
            frozen: vec![false; n + 2],
            to_right_nex: vec![idle_to_right; n + 2],
            to_left_nex: vec![idle_to_left; n + 2],
            ctrl_nex: vec![Ctrl::None; n + 2],
            signal_only,
            w_state,
            ft_state,
            ft2_state,
            cb_state,
            tr_state,
            t3_carriers,
            ng_carriers,
            p,
            d1,
            a3,
        };
        // 壁は故障ではない
        field.mask[0] = false;
        field.mask[n + 1] = false;
        field
    }

    // 1 ステップ: cur の状態から全セルの t+1 状態を next_cells に計算してから
    // ストリーム/制御チャネルを進める(更新順序に結果が依存しない)。
    fn fault_step(&mut self, current: &mut [u8], next_cells: &mut [u8], config: &Config) {
        let n = self.mask.len() - 2;

        // 壁の状態を次バッファへ保持する(--wallset で壁に外部信号を置くため。
        // 壁は逐次の計算対象外なので、このコピーがないと swap で www に戻る)
        next_cells[0] = current[0];
        next_cells[n + 1] = current[n + 1];

        // 1) 制御メッセージの到着処理。凍結/解凍は到着した時点で反映される
        //    (凍結されたセルはこのステップの状態変化が止まる)。
        for c in 1..=n {
            match self.ctrl[c] {
                Ctrl::Freeze => {
                    if !self.mask[c] {
                        self.frozen[c] = true;
                    }
                }
                Ctrl::Thaw => {
                    if !self.mask[c] {
                        self.frozen[c] = false;
                    }
                }
                Ctrl::None => {}
            }
        }

        // 2) 全セルの次状態を計算(故障セルはシフト、正常セルはルール表)
        for c in 1..=n {
            if self.mask[c] {
                // 故障セル: 左に由来する値を保ったまま右へ渡す準備をする。
                // 左隣の値は、左隣が故障ならその L→R ストリーム、正常ならそのセルの
                // 現在状態。A1 モードでは管路は信号だけを運ぶ: 流入口(隣が正常の
                // 場所)では計画された注入信号を取り込み、それ以外は www を充填する
                // (管からセル状態の生値を取り込まない)。
                let w = self.w_state;
                // 入口 pickup(0 成本: 境界セルの表示の時刻の次ステップに管路の
                // 先頭セルに入る = 論文の「反射の到着吸収」)。carrier(CB = 凍結
                // 起動 Carrier / TR = fast 継続の対岸横断)の表示のときだけ
                // 取り込み、それ以外はアイドル www。
                self.to_right_nex[c] = if self.mask[c - 1] {
                    self.to_right[c - 1]
                } else if self.signal_only {
                    let d = current[c - 1];
                    let cb_ok = self.d1.map(|(s, _)| c == s).unwrap_or(true);
                    // 入口 pickup: CB は最初の管路(I1 凍結起動 = fast 信号の始発)
                    // だけで取り込む(I_i (i>=2) の凍結起動 Carrier の表示を拾うと
                    // fast 信号が複製され、各区間の波と衝突する)。TR(fast 継続)は
                    // どこでも取り込んでよい(fast 継続そのもの)。NG 会合放出の
                    // 保値付き carrier {X}NG は会合の始発なので D1 入口だけで
                    // 取り込む(p=1 の管路と同じ制約)。
                    if (Some(d) == self.cb_state && cb_ok)
                        || Some(d) == self.tr_state
                        || (self.ng_carriers.contains(&d) && cb_ok)
                    {
                        d
                    } else {
                        // アイドル充填: L→R 管路は L を提示する((www,M,L) との
                        // 誤発火回避、new_ctx の idle_to_right と同じ理屈)
                        if self.signal_only { config.soldier as u8 } else { config.external as u8 }
                    }
                } else {
                    current[c - 1]
                };
                self.to_left_nex[c] = if self.signal_only && c > 1 && !self.mask[c - 1] {
                    // 近位口(左隣が正常セル)。入来信号を提示に優先させる:
                    // m >= 2 は右隣のストリーム中継、m == 1 は対岸の正常セルの
                    // 表示からの遠位口 pickup(解凍信号 FT を運ぶ)。
                    let inbound = if self.mask[c + 1] {
                        self.to_left[c + 1]
                    } else {
                        // m == 1: 対岸の正常セルの表示からの遠位口 pickup。
                        //   p=1: FT 変換(解凍)と TR (R2 装填の源流) とも有効。
                        //   p>=2(非 A3): TR (会合放出=R2 源流) のみ有効。残課題② で
                        //                区間識別をつけて FT 変換 pickup も開放する。
                        //   A3: 会合機械の時刻系がずれるので有効にしない。
                        let allowed = self.d1.map_or(true, |(_, e)| c == e)
                            && (self.p == 1
                                || (Some(current[c + 1]) == self.tr_state && !self.a3));
                        match if allowed { self.pickup_thaw(current[c + 1]) } else { None } {
                            Some(v) => v,
                            None => config.external as u8,
                        }
                    };
                    if inbound != config.external as u8 {
                        inbound
                    } else {
                        // 左口(I_i 右端セルの右入力)。FT の提示ステップの直後に
                        // アイドルを FT2(解凍後位相)へ切り替え、以後 FT2 を保つ。
                        // FR2...FT が入来した後の FT2 粘着も同じロジックで効く。
                        let cur = self.to_left[c];
                        if (self.ft2_state.is_some() && Some(cur) == self.ft_state)
                            || self.ft2_state == Some(cur)
                        {
                            self.ft2_state.unwrap()
                        } else if w.is_some() {
                            w.unwrap()
                        } else {
                            config.external as u8
                        }
                    }
                } else if self.mask[c + 1] {
                    self.to_left[c + 1]
                } else if self.signal_only {
                    // m >= 2 管路の遠位口 pickup(対岸の正常セルの表示から取り込む:
                    // 左走行解凍信号 FT / T3 / 複合 <X>T3 は p=1 かつ D1 の口のとき
                    // だけ。p>=2 では TR(R2 装填の源流)のみ、D1 の口のとき許す。
                    let allowed = self.d1.map_or(true, |(_, e)| c == e)
                        && (self.p == 1
                            || (Some(current[c + 1]) == self.tr_state && !self.a3));
                    match if allowed { self.pickup_thaw(current[c + 1]) } else { None } {
                        Some(v) => v,
                        None => config.external as u8,
                    }
                } else {
                    current[c + 1]
                };
                // 故障セルの基本状態は変わらない(遷移規則は使わない)
                next_cells[c] = current[c];
            } else {
                // ルール入力: 隣が故障セルなら、こちらへ向かって流れてきた
                // ストリーム値を見る(左隣からは L→R ストリーム、右隣からは
                // R→L ストリーム)。
                let left = if self.mask[c - 1] {
                    self.to_right[c - 1]
                } else {
                    current[c - 1]
                };
                let right = if self.mask[c + 1] {
                    self.to_left[c + 1]
                } else {
                    current[c + 1]
                };
                next_cells[c] = if self.frozen[c] {
                    current[c] // 凍結: 状態変化を止めて保持
                } else {
                    config.rules[((left as usize) << 14) + ((current[c] as usize) << 7) + (right as usize)]
                };
            }
        }

        // 3) 制御メッセージを左へ 1/1 で移動(frozen/故障/正常の区別なく通過する)
        for c in 1..=n {
            self.ctrl_nex[c] = if c < n { self.ctrl[c + 1] } else { Ctrl::None };
        }

        std::mem::swap(&mut self.to_right, &mut self.to_right_nex);
        std::mem::swap(&mut self.to_left, &mut self.to_left_nex);
        std::mem::swap(&mut self.ctrl, &mut self.ctrl_nex);
    }

    // 凍結状態のダンプ(-f/--inject 指定時のみ)。凍結セルに * を出す。
    // A1 モードでは、凍結変状態 F(x) を表示しているセル(ルール層で凍結)も * で
    // 示す(FA/FB/FC/FL/FM/FQ/FR = 2 文字の F で始まる状態名)。
    // 各行の列幅は状態行と揃えて位置を対応させる。
    fn dump_frozen(&self, current: &[u8], config: &Config) {
        let mut row = String::from("F:");
        for c in 1..current.len() - 1 {
            let width = config.states[current[c] as usize].name.len();
            let rule_frozen = if self.frozen[c] {
                true
            } else {
                let name = config.states[current[c] as usize].name.trim();
                self.intact(c)
                    && name.len() == 2
                    && name.starts_with('F')
                    && matches!(name.chars().nth(1), Some('A' | 'B' | 'C' | 'L' | 'M' | 'Q' | 'R'))
            };
            let text = if rule_frozen { "*".to_string() } else { String::new() };
            row.push('|');
            if text.chars().count() < width {
                row.push_str(&" ".repeat(width - text.chars().count()));
            }
            row.push_str(&text);
        }
        row.push('|');
        println!("{}", row);
    }

    // ストリーム内容のダンプ(故障区間のあるときのみ呼ぶ。各行の列幅は状態行と
    // 揃えて位置を対応させる)。正常セルは空白。
    fn dump_streams(&self, current: &[u8], config: &Config, prefix: &str) {
        let mut row = String::from(prefix);
        for c in 1..current.len() - 1 {
            let width = config.states[current[c] as usize].name.len();
            let text: String = if self.mask[c] {
                let value = if prefix == "R:" { self.to_right[c] } else { self.to_left[c] };
                let name = &config.states[value as usize].name;
                format!("[{}]", name)
            } else {
                String::new()
            };
            if text.chars().count() < width {
                row.push('|');
                row.push_str(&" ".repeat(width - text.chars().count()));
                row.push_str(&text);
            } else {
                row.push('|');
                row.push_str(&text);
            }
        }
        row.push('|');
        println!("{}", row);
    }
}

// 同期成功の判定は正常セルのみで行う(故障セルは発火状態に遷移しない)
fn fired_intact(cells: &[u8], mask: &[bool], firing: usize) -> bool {
    for i in 1..cells.len() - 1 {
        if !mask[i] && cells[i] as usize != firing {
            return false;
        }
    }
    true
}

// 故障ありの逐次シミュレーション。injections は (種類, セル, 時刻)、wallsets は
// (状態番号, 時刻)で時刻 t に右壁の状態を置き換える(--wallset: 論文モデルの
// 「外部からの信号」= 壁側から提示される信号状態)。
// signals は A1/A2 スケジュール信号(plan_a1_signals の出力)。時刻 t に管路の
// 流入口セルへ値を置き、1/1 で流れて近位口で提示される。
// メッセージは「時刻 t にその時刻のセルに置かれる=そのステップで到着処理される」。
// 戻り値は終了した時刻(正常セル全発火または上限)。
fn simulate_fault(
    current: &mut Vec<u8>,
    next_cells: &mut Vec<u8>,
    ff: &mut FaultField,
    config: &Config,
    cap: usize,
    injections: &[(Ctrl, usize, usize)],
    wallsets: &[(u8, usize)],
    signals: &[PlannedSignal],
    dump: bool,
    color: bool,
) -> usize {
    let n = current.len() - 2;
    let mut t = 0;
    loop {
        // この時刻の信号注入(管路の流入口セルに値を置く→ 1/1 で流れて近位口に提示)
        for sig in signals {
            if sig.time == t {
                if sig.to_left {
                    ff.to_left[sig.cell] = sig.value;
                } else {
                    ff.to_right[sig.cell] = sig.value;
                }
            }
        }
        for &(kind, cell, time) in injections {
            if time == t {
                ff.ctrl[cell] = kind;
            }
        }
        for &(state, time) in wallsets {
            if time == t {
                current[n + 1] = state;
            }
        }
        if dump {
            // 時刻 t の状態(その時刻に提示される信号を含む管路内容)を出す
            dump_line(current, config, color);
            if !color {
                if ff.mask.iter().any(|&m| m) {
                    ff.dump_streams(current, config, "R:");
                    ff.dump_streams(current, config, "L:");
                }
                if ff.frozen.iter().any(|&f| f) || (1..=(current.len() - 2)).any(|c| ff.intact(c) && {
                    let name = config.states[current[c] as usize].name.trim();
                    name.len() == 2
                        && name.starts_with('F')
                        && matches!(name.chars().nth(1), Some('A' | 'B' | 'C' | 'L' | 'M' | 'Q' | 'R'))
                }) {
                    ff.dump_frozen(current, config);
                }
            }
        }
        if fired_intact(current, &ff.mask, config.firing) || t >= cap {
            break;
        }
        // 次状態を next_cells に計算してから入れ替える(2 バッファ交互)
        ff.fault_step(&mut current[..], &mut next_cells[..], config);
        std::mem::swap(current, next_cells);
        t += 1;
    }
    t
}

// 注入セルの範囲検証(セル番号は実セル内)
fn validate_injections(injections: &[(Ctrl, usize, usize)], n: usize) -> Vec<(Ctrl, usize, usize)> {
    for &(kind, cell, _) in injections {
        if cell > n {
            let kind = match kind {
                Ctrl::Freeze => "freeze",
                Ctrl::Thaw => "thaw",
                Ctrl::None => "none",
            };
            eprintln!("--inject: cell out of range 1..{}: {}@{}", n, kind, cell);
            eprintln!("{}", USAGE);
            std::process::exit(1);
        }
    }
    injections.to_vec()
}

const USAGE: &str = "usage: fssp -r <rulefile> [-s size] [-f faults] [--a3] [--inject ...] [--wallset ...] [-t steps] [--strict] [-d] [-c]";

fn main() {
    let args: Vec<String> = env::args().collect();

    // 使い方: fssp.exe -r <rulefile> [-s size] [-f faults] [--strict] [-d] [-c]
    //   -r / --rule   : 遷移規則ファイル(必須)
    //   -s / --size   : セル数(既定 10)
    //   -f / --fault  : 故障区間リスト(1 始まり、カンマ区切り、a-b は a..b)。
    //                   隣接する区間は 1 つの故障区間に統合される
    //   --strict      : 故障配置が n_i >= m_i (Umeo A1/A2 の条件) を満たさない
    //                   場合に実行せず終了する(無指定時は警告して続行)
    //   --inject ...  : 制御メッセージの注入(デバッグ用)。freeze|thaw@セル@時刻。
    //                   複数可。--fault なしでも Lemma 1 検証用に使える
    //   -t / --steps  : 故障あり実行のステップ上限(既定 2n-2+最大注入時刻)
    //   -d / --dump   : 逐次実行して 1 ステップごとに途中経過を出力
    //                  (ruby の -d と同じ書式。
    //                   並行実行のダンプとは干渉するため逐次にする。
    //                   -f/--inject 時は R:/L: ストリーム行と F: 凍結行を追加)
    //   -c / --color  : ダンプを dumpln と同じ ANSI 色付きに
    //                  (-c 単独指定時は -d を兼ねる)
    let mut dump_mode = false;
    let mut color_mode = false;
    let mut strict_mode = false;
    let mut a3_mode = false;
    let mut size_arg: Option<usize> = None;
    let mut rule_arg: Option<String> = None;
    let mut fault_arg: Option<String> = None;
    let mut steps_arg: Option<usize> = None;
    let mut injections: Vec<(Ctrl, usize, usize)> = vec![];
    let mut wallsets: Vec<(String, usize)> = vec![];

    // 位置引数は取らず、既定のルールファイルも持たない(必ず -r で指定する)。
    // 解釈できない引数や値の欠落はエラーにする。以前は数値に解釈できない位置引数を
    // 黙って捨てていたため、`fssp rulefile 5` と打つと既定の waksman で走ってしまい
    // 別のルールセットの発火と紛らわしかった。
    // 位置引数は取らず、既定のルールファイルも持たない(必ず -r で指定する)。
    // 解釈できない引数や値の欠落はエラーにする。以前は数値に解釈できない位置引数を
    // 黙って捨てていたため、`fssp rulefile 5` と打つと既定の waksman で走ってしまい
    // 別のルールセットの発火と紛らわしかった。
    const OPTIONS: &str = concat!(
        "  -r, --rule <file>     遷移規則ファイル(必須)\n",
        "  -s, --size <n>        セル数(既定 10)\n",
        "  -f, --fault <list>    故障区間リスト(1 始まり、カンマ区切り、例: 3-5,10)\n",
        "  --inject <kind@c@t>   制御メッセージ注入(デバッグ用、複数可)\n",
        "                        kind: freeze|thaw、c: セル番号、t: 注入時刻\n",
        "  --wallset <state@t>   時刻 t に右壁の提示状態を state に変える\n",
        "                        (論文モデルの「外部からの信号」。複数可)\n",
        "  -t, --steps <k>       故障あり実行のステップ上限(既定 2n-2+最大注入時刻)\n",
        "  --strict              n_i>=m_i (A1/A2 条件) を満たす配置以外は実行しない\n",
        "  -d, --dump            1 ステップごとの途中経過を出力(ruby -d と同書式)\n",
        "                        (-f/--inject 時は R:/L:/F: 凍結行も追加)\n",
        "  -c, --color           ダンプを ANSI 色付きに(-c は -d を兼ねる)\n",
        "  -h, --help            このメッセージ"
    );

    // -r / -s / -f / --inject / -t は値を取るので index で回す
    let mut i = 1;
    while i < args.len() {
        let opt = args[i].as_str();
        match opt {
            "-h" | "--help" => {
                // エラーではないので stdout に出して正常終了する
                println!("{}\n{}", USAGE, OPTIONS);
                std::process::exit(0);
            }
            "-d" | "--dump" => dump_mode = true,
            "-c" | "--color" => {
                color_mode = true;
                dump_mode = true;
            }
            "--strict" => strict_mode = true,
            "--a3" => a3_mode = true,
            "-r" | "--rule" | "-s" | "--size" | "-f" | "--fault" | "--inject" | "--wallset" | "-t" | "--steps" => {
                i += 1;
                let value = match args.get(i) {
                    Some(v) => v,
                    None => {
                        eprintln!("{}: value is required", opt);
                        eprintln!("{}", USAGE);
                        std::process::exit(1);
                    }
                };

                if opt == "-r" || opt == "--rule" {
                    rule_arg = Some(value.clone());
                } else if opt == "-s" || opt == "--size" {
                    match value.parse::<usize>() {
                        Ok(n) => size_arg = Some(n),
                        Err(_) => {
                            eprintln!("{}: invalid size: {}", opt, value);
                            eprintln!("{}", USAGE);
                            std::process::exit(1);
                        }
                    }
                } else if opt == "-f" || opt == "--fault" {
                    fault_arg = Some(value.clone());
                } else if opt == "--wallset" {
                    // --wallset STATE@t: 時刻 t に右壁の提示状態を STATE に変える
                    let parts: Vec<&str> = value.split('@').collect();
                    let (name, tt) = match (parts.as_slice(), parts.len()) {
                        ([n_, t], 2) => (n_, t),
                        _ => {
                            eprintln!("{}: invalid wallset (expect state@time): {}", opt, value);
                            eprintln!("{}", USAGE);
                            std::process::exit(1);
                        }
                    };
                    let tt = match tt.parse::<usize>() {
                        Ok(v) => v,
                        Err(_) => {
                            eprintln!("{}: invalid time: {}", opt, tt);
                            eprintln!("{}", USAGE);
                            std::process::exit(1);
                        }
                    };
                    wallsets.push((name.to_string(), tt));
                } else if opt == "-t" || opt == "--steps" {
                    match value.parse::<usize>() {
                        Ok(k) if k > 0 => steps_arg = Some(k),
                        _ => {
                            eprintln!("{}: invalid step count: {}", opt, value);
                            eprintln!("{}", USAGE);
                            std::process::exit(1);
                        }
                    }
                } else {
                    // --inject kind@cell@time
                    let parts: Vec<&str> = value.split('@').collect();
                    let (kind, cell, time) = match (parts.as_slice(), parts.len()) {
                        ([k, c, t], 3) => (k, c, t),
                        _ => {
                            eprintln!("{}: invalid injection (expect kind@cell@time): {}", opt, value);
                            eprintln!("{}", USAGE);
                            std::process::exit(1);
                        }
                    };
                    let kind = match *kind {
                        "freeze" | "f" => Ctrl::Freeze,
                        "thaw" | "t" => Ctrl::Thaw,
                        _ => {
                            eprintln!("{}: invalid kind (freeze|thaw): {}", opt, kind);
                            eprintln!("{}", USAGE);
                            std::process::exit(1);
                        }
                    };
                    let cell = match cell.parse::<usize>() {
                        Ok(c) if c >= 1 => c,
                        _ => {
                            eprintln!("{}: invalid cell: {}", opt, cell);
                            eprintln!("{}", USAGE);
                            std::process::exit(1);
                        }
                    };
                    let time = match time.parse::<usize>() {
                        Ok(t) => t,
                        _ => {
                            eprintln!("{}: invalid time: {}", opt, time);
                            eprintln!("{}", USAGE);
                            std::process::exit(1);
                        }
                    };
                    injections.push((kind, cell, time));
                }
            }
            v => {
                eprintln!("unknown argument: {}", v);
                eprintln!("{}", USAGE);
                std::process::exit(1);
            }
        }
        i += 1;
    }

    let cell_size = size_arg.unwrap_or(10);

    let rule_file = match rule_arg {
        Some(path) => path,
        None => {
            eprintln!("rule file is not specified");
            eprintln!("{}", USAGE);
            std::process::exit(1);
        }
    };

    let s = match read_file(rule_file.clone()) {
        Ok(s) => s,
        Err(e) => panic!("fail to read file {}: {}", rule_file, e),
    };

    let mut config = parse_rule_file(&s);

    for (i, state) in config.states.iter().enumerate() {
        match state.class.as_str() {
            "general" => config.general = i,
            "soldier" => config.soldier = i,
            "external" => config.external = i,
            "firing" => config.firing = i,
            _ => {}
        }
    }

    // 故障も制御メッセージ注入も壁信号もなければ通常シミュレーションへ。
    // --wallset 単独(壁から凍結/解凍信号を提示)は Lemma 1 の遅延同期の検証用。
    if fault_arg.is_some() || !injections.is_empty() || !wallsets.is_empty() {
        let analysis = match fault_arg.as_ref() {
            None => None,
            Some(spec) => {
                let fault = match parse_fault_spec(spec, cell_size) {
                    Ok(f) => f,
                    Err(e) => {
                        eprintln!("--fault: {}", e);
                        eprintln!("{}", USAGE);
                        std::process::exit(1);
                    }
                };

                let def_str: Vec<String> = fault
                    .segments
                    .iter()
                    .map(|&(a, b)| {
                        if a == b {
                            format!("{}", a)
                        } else {
                            format!("{}-{}", a, b)
                        }
                    })
                    .collect();
                println!("cells: {}, defective: {}", cell_size, def_str.join(","));

                let analysis = analyze_faults(&fault, cell_size);
                let (fail_a1a2, fail_a3c) = report_faults(&analysis);

                if !fail_a1a2.is_empty() {
                    eprintln!(
                        "warning: fault layout violates n_i>=m_i; sync guarantee of Umeo A1/A2 does not apply"
                    );
                }
                if a3_mode && !fail_a3c.is_empty() {
                    eprintln!(
                        "warning: layout violates n_i+m_i>=p-i (Umeo A3 condition, Theorem 4); A3 sync guarantee does not apply"
                    );
                }
                if strict_mode
                    && (!fail_a1a2.is_empty() || (a3_mode && !fail_a3c.is_empty()))
                {
                    eprintln!("--strict: refusing to run");
                    std::process::exit(1);
                }
                Some(analysis)
            }
        };

        let mask = match &analysis {
            Some(a) => a.mask.clone(),
            None => vec![false; cell_size + 2],
        };

        // 故障あり/制御注入は逐次実行する(制御層・中継は HALO をまたぐ隣接参照を
        // 含み、並列パスのトークン同期設計とは別の調整が必要)。
        let injections = validate_injections(&injections, cell_size);
        // A1 モード: -f 指定で --inject/--wallset なし。管路は信号だけを運び、
        // Umeo A1/A2 の式から区間構造より算出した時刻で凍結/解凍/将軍起動を提示する。
        let signal_only = fault_arg.is_some() && injections.is_empty() && wallsets.is_empty();
        // to_left 管路の変換 pickup は p=1 のときだけ有効(他区間の反射波が
        // 隔離: to_left 管路の FT 変換 pickup は p=1 の D1 だけ有効(p>=2 では
        // 各区間の解凍を注入 FT が担う。他区間の反射波が解凍を起動するのを防ぐ)。
        // to_right 入口の CB pickup も D1 だけ(fast 信号は 1 本なので)。
        let d1 = analysis
            .as_ref()
            .map(|a| (a.segments[1].start, a.segments[1].end));
        let p_cnt = analysis
            .as_ref()
            .map(|a| a.segments.iter().filter(|s| !s.intact).count())
            .unwrap_or(0);
        let mut ff = FaultField::new_ctx(&mask, &config, signal_only, p_cnt, d1, a3_mode);

        // A1/A2 の信号スケジュールを区間構造から組み立てる
        let signals: Vec<PlannedSignal> = match &analysis {
            Some(a) if signal_only => {
                let (plan, notes) = plan_a1_signals(a, &config, a3_mode);
                for note in &notes {
                    println!("a1: {}", note);
                }
                plan
            }
            _ => vec![],
        };

        // --wallset の状態名をルールファイルの状態番号に解決する
        let wallsets: Vec<(u8, usize)> = wallsets
            .into_iter()
            .map(|(name, t)| {
                match config.states.iter().position(|s| s.name.trim() == name) {
                    Some(idx) => (idx as u8, t),
                    None => {
                        eprintln!("--wallset: unknown state: {}", name);
                        eprintln!("{}", USAGE);
                        std::process::exit(1);
                    }
                }
            })
            .collect();

        let seg_count = analysis
            .as_ref()
            .map(|a| a.segments.iter().filter(|s| !s.intact).count())
            .unwrap_or(0);
        let base_cap = if a3_mode {
            // A3 の同期時刻は 2n-2+p (Theorem 4)
            (cell_size << 1) - 2 + seg_count
        } else {
            (cell_size << 1) - 2
        };
        let max_t = injections
            .iter()
            .map(|&(_, _, t)| t)
            .chain(wallsets.iter().map(|&(_, t)| t))
            .max()
            .unwrap_or(0);
        let cap = steps_arg.unwrap_or(base_cap + max_t);

        let start = Instant::now();
        let mut current = first_line(cell_size, &config);
        let mut next_cells = new_line(cell_size, &config);
        let t = simulate_fault(
            &mut current,
            &mut next_cells,
            &mut ff,
            &config,
            cap,
            &injections,
            &wallsets,
            &signals,
            dump_mode,
            color_mode,
        );

        if fired_intact(&current, &mask, config.firing) {
            println!("SYNCED (intact cells) at t={}", t);
        } else {
            println!("NOT SYNCED (stopped at t={})", t);
        }

        let end = start.elapsed();
        println!(
            "time: {}.{:03}s",
            end.as_secs(),
            end.subsec_nanos() / 1_000_000
        );
        std::process::exit(0);
    }

    #[cfg(target_arch = "x86_64")]
    let use_avx2 = is_x86_feature_detected!("avx2");
    #[cfg(not(target_arch = "x86_64"))]
    let use_avx2 = false;

    let mut current = first_line(cell_size, &config);
    let mut next_cells = new_line(cell_size, &config);

    let start = Instant::now();
    let end;

    if dump_mode {
        // ダンプ本体が初期状態と最終状態を出すので、debug 表示は重複になる
        // この分岐では出さない
        simulate_dump(&mut current, &mut next_cells, &config, cell_size, color_mode);
        end = start.elapsed();
    } else {
        #[cfg(debug_assertions)]
        dumpln(&current, &config);

        par_simulate(&mut current[..], &mut next_cells[..], &config, cell_size, use_avx2);

        if !fired(&current, config.firing) {
            println!("NOT SYNCED");
        }

        end = start.elapsed();

        #[cfg(debug_assertions)]
        dumpln(&current, &config);
    }

    println!(
        "time: {}.{:03}s",
        end.as_secs(),
        end.subsec_nanos() / 1_000_000
    );
}#[cfg(test)]
mod tests {
    use super::*;

    fn st(name: &str, class: &str) -> State {
        State {
            name: name.to_string(),
            foreground: "ffffff".to_string(),
            background: "000000".to_string(),
            class: class.to_string(),
        }
    }

    // 合成ルールセット(単体テスト用):
    //   状態 0=www(external) 1=Q(soldier) 2=S(自走信号) 3=G(general)
    //   S は Q の列を左右両方向に 1/1 で進む。中心 S は 1 ステップで Q に戻り、
    //   周囲の Q は「いずれかの隣が S なら S になる」。
    fn tiny_config() -> Config {
        let states = vec![
            st("www", "external"),
            st("Q", "soldier"),
            st("S", ""),
            st("G", "general"),
        ];
        let mut rules: Box<[u8; 1 << 21]> =
            vec![0u8; 1 << 21].into_boxed_slice().try_into().unwrap();
        for l in 0u8..4 {
            for r in 0u8..4 {
                // 中心 Q: 隣に S がいれば S になる(両方向 1/1 進行)、いなければ Q
                rules[((l as usize) << 14) + (1 << 7) + (r as usize)] =
                    if l == 2 || r == 2 { 2 } else { 1 };
                // 中心 S: 発信して Q に戻る
                rules[((l as usize) << 14) + (2 << 7) + (r as usize)] = 1;
            }
        }
        Config {
            states,
            general: 3,
            soldier: 1,
            firing: 3,
            external: 0,
            rules,
        }
    }

    fn fault_mask(n: usize, a: usize, b: usize) -> Vec<bool> {
        let mut mask = vec![false; n + 2];
        for c in a..=b {
            mask[c] = true;
        }
        mask
    }

    fn run_steps(
        cur: &mut Vec<u8>,
        nex: &mut Vec<u8>,
        ff: &mut FaultField,
        cfg: &Config,
        k: usize,
    ) {
        for _ in 0..k {
            ff.fault_step(&mut cur[..], &mut nex[..], cfg);
            std::mem::swap(cur, nex);
        }
    }

    // L→R: S を故障区間(5-6)の手前セルに置くと、区間を 1/1 で渡って対岸の
    // 正常セルが反応し、以降も 1/1 で進む
    #[test]
    fn relay_right_crosses_defect() {
        let cfg = tiny_config();
        let n = 10;
        let mut cur = new_line(n, &cfg);
        let mut nex = new_line(n, &cfg);
        let mut ff = FaultField::new(&fault_mask(n, 5, 6), &cfg, false);
        cur[4] = 2; // セル 4 に S

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 2);
        assert_eq!(ff.to_right[6], 2, "t=2: 区間右端が対岸に S を提示");
        assert_eq!(cur[7], 1, "t=2: 対岸セルはまだ反応していない");
        assert_eq!(cur[5], 1, "故障セルの基本状態は不変");

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert_eq!(cur[7], 2, "t=3: 対岸セルが S で反応(全区間 1/1)");
        assert_eq!(ff.to_right[6], 1, "t=3: 通過後は Q が流れている");

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert_eq!(cur[8], 2, "t=4: 対岸で S が継続して進む");
    }

    // R→L 逆方向も同じ 1/1
    #[test]
    fn relay_left_crosses_defect() {
        let cfg = tiny_config();
        let n = 10;
        let mut cur = new_line(n, &cfg);
        let mut nex = new_line(n, &cfg);
        let mut ff = FaultField::new(&fault_mask(n, 5, 6), &cfg, false);
        cur[8] = 2; // セル 8 に S

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 3);
        assert_eq!(ff.to_left[5], 2, "t=3: 区間左端が対岸に S を提示");
        assert_eq!(cur[4], 1);

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert_eq!(cur[4], 2, "t=4: 対岸セルが S で反応(全区間 1/1)");
    }

    // 逆方向 2 信号が同時に交差しても干渉しない
    #[test]
    fn relay_both_directions_simultaneously() {
        let cfg = tiny_config();
        let n = 10;
        let mut cur = new_line(n, &cfg);
        let mut nex = new_line(n, &cfg);
        let mut ff = FaultField::new(&fault_mask(n, 5, 6), &cfg, false);
        cur[4] = 2;
        cur[8] = 2;

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 3);
        assert_eq!(ff.to_left[5], 2, "t=3: R→L ストリームの区間左端に S");
        assert_eq!(cur[7], 2, "t=3: 右からの S を対岸で受け取り");
        assert_eq!(cur[4], 1, "左側はまだ反応していない");

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert_eq!(cur[4], 2, "t=4: 左からの S を対岸で受け取り");
    }

    // 故障あり・信号なし: 何も起きない(Q は Q のまま、ストリームも Q)
    #[test]
    fn idle_stays_quiescent() {
        let cfg = tiny_config();
        let n = 10;
        let mut cur = new_line(n, &cfg);
        let mut nex = new_line(n, &cfg);
        let mut ff = FaultField::new(&fault_mask(n, 5, 6), &cfg, false);

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 5);
        for c in 1..=n {
            assert_eq!(cur[c], 1, "cell {}", c);
        }
        for c in 5..=6 {
            assert_eq!(ff.to_right[c], 1);
            assert_eq!(ff.to_left[c], 1);
        }
    }

    // 凍結信号は 1/1 左向きに伝わり、到着したセルを順に凍結する
    #[test]
    fn freeze_progressively() {
        let cfg = tiny_config();
        let n = 8;
        let mut cur = new_line(n, &cfg);
        let mut nex = new_line(n, &cfg);
        let mut ff = FaultField::new(&fault_mask(n, 0, 0), &cfg, false); // 故障なし
        ff.ctrl[4] = Ctrl::Freeze; // t=0 にセル 4 に凍結メッセージ

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert!(ff.frozen[4], "t=1: セル4凍結");
        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert!(ff.frozen[3], "t=2: セル3凍結");
        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert!(ff.frozen[2], "t=3: セル2凍結");
        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert!(ff.frozen[1], "t=4: セル1凍結(左端到達)");

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        for c in 1..=n {
            assert_eq!(ff.ctrl[c], Ctrl::None, "t=5: メッセージは壁で消滅");
        }
    }

    // 凍結されたセルの前で信号は消える(凍結セルは状態変化を止めて保持)
    #[test]
    fn freeze_blocks_signal() {
        let cfg = tiny_config();
        let n = 8;
        let mut cur = new_line(n, &cfg);
        let mut nex = new_line(n, &cfg);
        let mut ff = FaultField::new(&fault_mask(n, 0, 0), &cfg, false);
        ff.ctrl[4] = Ctrl::Freeze; // t=0: セル 4 を凍結
        cur[3] = 2; // t=0: セル 3 に S(右へ進もうとする)

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert_eq!(cur[4], 1, "t=1: 凍結セルは反応せず Q を保持");
        assert!(ff.frozen[4]);
        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 2);
        assert_eq!(cur[4], 1, "t=3: 保持を維持");
        assert_eq!(cur[5], 1, "信号は凍結面の先へ進んでいない");
    }

    // 制御メッセージは故障区間を凍結せず通過する
    #[test]
    fn control_crosses_defective_segment() {
        let cfg = tiny_config();
        let n = 10;
        let mut cur = new_line(n, &cfg);
        let mut nex = new_line(n, &cfg);
        let mut ff = FaultField::new(&fault_mask(n, 5, 6), &cfg, false);
        ff.ctrl[7] = Ctrl::Freeze; // t=0: セル 7(区間の右隣)に注入

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 1);
        assert!(ff.frozen[7], "t=1: セル7凍結");
        assert!(!ff.frozen[5] && !ff.frozen[6], "故障セルは凍結しない");
        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 3);
        assert!(ff.frozen[4], "t=4: メッセージは区間を渡って対岸も凍結");
        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 3);
        assert!(ff.frozen[1], "t=7: 左端まで到達");
    }

    // 解凍後は信号の通過を再び受け付ける(再開の証明)
    #[test]
    fn thaw_resumes_signal_passing() {
        let cfg = tiny_config();
        let n = 8;
        let mut cur = new_line(n, &cfg);
        let mut nex = new_line(n, &cfg);
        let mut ff = FaultField::new(&fault_mask(n, 0, 0), &cfg, false);
        ff.ctrl[4] = Ctrl::Freeze; // t=0: セル4を凍結

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 3);
        assert!(ff.frozen[4], "t=3: セル4は凍結中");
        ff.ctrl[4] = Ctrl::Thaw; // t=3 に解凍メッセージを置く

        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 3);
        assert!(!ff.frozen[4], "t=6: 解凍済み");

        // 解凍後に S を置くと、凍結だったセル 4 も通過を認める
        cur[2] = 2;
        run_steps(&mut cur, &mut nex, &mut ff, &cfg, 2);
        assert_eq!(cur[4], 2, "t=8: 解凍されたセル 4 が信号を通過させた");
    }
}
#[cfg(test)]
mod probe_tests {
    use super::*;

    #[test]
    fn waksman_rule_index_check() {
        let s = read_file("../waksman-slim.rul.txt".to_string()).unwrap();
        let mut config = parse_rule_file(&s);
        for (i, state) in config.states.iter().enumerate() {
            match state.class.as_str() {
                "general" => config.general = i,
                "soldier" => config.soldier = i,
                "external" => config.external = i,
                "firing" => config.firing = i,
                _ => {}
            }
        }
        assert_eq!(config.general, 5, "P0 index");
        assert_eq!(config.external, 15, "www index");
        let got = nextcell(config.external as u8, config.general as u8, config.soldier as u8, &config);
        assert_eq!(got as usize, config.general, "rule (www,P0,Q) must yield P0");
    }

    #[test]
    fn waksman_full_firing_seq() {
        use std::time::Instant;
        let s = read_file("../waksman-slim.rul.txt".to_string()).unwrap();
        let mut config = parse_rule_file(&s);
        for (i, state) in config.states.iter().enumerate() {
            match state.class.as_str() {
                "general" => config.general = i,
                "soldier" => config.soldier = i,
                "external" => config.external = i,
                "firing" => config.firing = i,
                _ => {}
            }
        }
        let _ = Instant::now();
        let mut cur = first_line(5, &config);
        let mut nex = new_line(5, &config);
        let mut t = 0;
        while !fired(&cur, config.firing) && t <= 2 * 5 - 2 {
            nextline(&mut cur, &mut nex, &config);
            std::mem::swap(&mut cur, &mut nex);
            t += 1;
        }
        assert_eq!(t, 8, "fires at 2n-2");
    }
}
