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
    // 9 ビット/次元のインデックス (l<<18)+(c<<9)+r(表 2^27 要素 × u16 = 256 MB、
    // 遅延ゼロ初期化なので触れたページだけ常駐)。状態数最大 512(凍結層の複合
    // 状態のために 32→64→128→256 から拡張。セル状態は u16 = St)。表は末尾に
    // 2 要素の余白を持つ(AVX2 gather の 4 バイト読み出し用)。
    rules: Vec<St>,
    // 規則が明示されている鍵のビット集合(診断用: 未定義鍵の FB 落ちを検出)
    defined: Vec<u64>,
    // ── 純粋シフト線モード(--pure、(a) の模型)の規則表 ──
    // 故障セルは生の状態を 1/1 で写すだけなので、判断は全て境界セル(左隣または
    // 右隣が故障の正常セル)の規則で行う。境界セルの規則 f_B(l,c,r) は
    //   l' = 左隣が故障なら PL(l) else l、r' = 右隣が故障なら PR(r) else r として
    //   bmap[(flags,l',c,r')] があればそれ、無ければ rules[l'][c][r']
    // (PL/PR は故障側の生の入力の射影=信号の分類)。
    // 境界専用の状態を内部セルが入力に見るときは PJ で基底に射影する。
    // 規則ファイルの行 `PL:a->b` `PR:a->b` `PJ:a->b` `B1:l##c##r->t`(左隣が故障)
    // `B2:l##c##r->t`(右隣が故障)で与える。無ければ恒等。
    pl: Vec<St>,
    pr: Vec<St>,
    pj: Vec<St>,
    bmap: std::collections::HashMap<(u8, St, St, St), St>,
}

// セル状態の型と規則表の索引幅
type St = u16;
const BITS: usize = 9;
const TBL: usize = 1 << (3 * BITS);

fn dump_full(cells: &[St], config: &Config) {
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

fn dumpln(cells: &[St], config: &Config) {
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

fn dump(cells: &[St], config: &Config) {
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

fn dumpright(cells: &[St], config: &Config) {
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

fn calc_next(next_cells: &mut [St], idx: usize, current: &[St], config: &Config) {
    let mut i = idx;
    for cells in current.windows(3) {
        next_cells[i] = nextcell(cells[0], cells[1], cells[2], config);
        i += 1;
    }
}

fn nextcell(left: St, center: St, right: St, config: &Config) -> St {
    config.rules[((left as usize) << (2 * BITS)) + ((center as usize) << BITS) + (right as usize)]
}

fn nextline(cur: &mut [St], nex: &mut [St], config: &Config) {
    calc_next(nex, 1, cur, config);
}

// AVX2 gather パス(u16 セル、9 ビット/次元の索引)。8 セルずつ u32 に拡張して
// 索引 (l<<18)+(c<<9)+r を作り、u16 表を scale=2 で gather した dword の下位
// 16 ビットを状態として回収する(表は末尾に 2 要素の余白を持つ)。
#[cfg(target_arch = "x86_64")]
mod simd {
    use super::{St, BITS};
    use std::arch::x86_64::*;

    #[target_feature(enable = "avx2")]
    pub unsafe fn step(cur: &[St], nex: &mut [St], rules: &[St]) {
        let len = cur.len();
        let mask16 = _mm256_set1_epi32(0xFFFF);
        let mut i = 1usize;
        let last = len - 1; // 中心セルの排他上限
        while i + 8 <= last {
            let base = cur.as_ptr().add(i - 1);
            let l = _mm256_cvtepu16_epi32(_mm_loadu_si128(base as *const __m128i));
            let c = _mm256_cvtepu16_epi32(_mm_loadu_si128(base.add(1) as *const __m128i));
            let r = _mm256_cvtepu16_epi32(_mm_loadu_si128(base.add(2) as *const __m128i));
            let idx = _mm256_or_si256(
                _mm256_or_si256(
                    _mm256_slli_epi32(l, (2 * BITS) as i32),
                    _mm256_slli_epi32(c, BITS as i32),
                ),
                r,
            );
            let g = _mm256_and_si256(
                _mm256_i32gather_epi32(rules.as_ptr() as *const i32, idx, 2),
                mask16,
            );
            // 8 x u32 → 8 x u16(packus は 128 ビットレーン内で畳むので並べ直す)
            let p = _mm256_packus_epi32(g, g);
            let p = _mm256_permute4x64_epi64(p, 0b00_00_10_00);
            _mm_storeu_si128(nex.as_mut_ptr().add(i) as *mut __m128i, _mm256_castsi256_si128(p));
            i += 8;
        }
        // 端数はスカラーで
        while i < last {
            nex[i] = rules[((cur[i - 1] as usize) << (2 * BITS))
                + ((cur[i] as usize) << BITS)
                + (cur[i + 1] as usize)];
            i += 1;
        }
    }
}

fn nextline_par(ary_pair: &mut AryPair, config: &Config, use_avx2: bool) {
    #[cfg(target_arch = "x86_64")]
    {
        if use_avx2 {
            unsafe { simd::step(ary_pair.cur, ary_pair.nex, &config.rules[..]) };
            return;
        }
    }
    let _ = use_avx2;
    calc_next(ary_pair.nex, 1, ary_pair.cur, config);
}

fn per_nextline(current: &[St], next_cells: &mut [St], config: &Config) {
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

fn parse_rules(
    line: &str,
    n: i32,
    name_idx: &std::collections::HashMap<String, usize>,
) -> (ParseState, usize, usize) {
    let (lhs, rhs) = line.split_once("->").unwrap_or((line, ""));
    let mut idx: usize = 0;
    for rule_state in lhs.split("##") {
        let i = *name_idx.get(rule_state).unwrap_or(&0);
        idx = (idx << BITS) + i;
    }
    let n_idx = *name_idx.get(rhs).unwrap_or(&0);

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
        rules: vec![0 as St; TBL + 2],
        defined: vec![0u64; TBL / 64],
        general: 0,
        soldier: 0,
        firing: 0,
        external: 0,
        pl: vec![],
        pr: vec![],
        pj: vec![],
        bmap: std::collections::HashMap::new(),
    };

    // 状態名 → 番号(規則行の各レーンを O(1) で引く。線形探索だと 300 万行で
    // 数秒かかる)。状態の宣言は規則より前に来るので、規則行に入る時点で作る。
    let mut name_idx: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    let mut name_trim: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for line in s.lines() {
        parse_state = match parse_state {
            ParseState::Begin => parse_begin(&line),
            ParseState::FoundState(n) => {
                let (next_state, state) = parse_states(&line, n);
                config.states.push(state);
                next_state
            }
            ParseState::FoundRule(n) => {
                if name_idx.len() != config.states.len() {
                    name_idx = config
                        .states
                        .iter()
                        .enumerate()
                        .map(|(i, st)| (st.name.clone(), i))
                        .collect();
                    name_trim = config
                        .states
                        .iter()
                        .enumerate()
                        .map(|(i, st)| (st.name.trim().to_string(), i))
                        .collect();
                    let ns = config.states.len();
                    config.pl = (0..ns).map(|i| i as St).collect();
                    config.pr = (0..ns).map(|i| i as St).collect();
                    config.pj = (0..ns).map(|i| i as St).collect();
                }
                let next_state = if n == 1 { ParseState::Begin } else { ParseState::FoundRule(n - 1) };
                let lt = line.trim();
                let (kind, rest) = if let Some(r) = lt.strip_prefix("PL:") {
                    ('L', r)
                } else if let Some(r) = lt.strip_prefix("PR:") {
                    ('R', r)
                } else if let Some(r) = lt.strip_prefix("PJ:") {
                    ('J', r)
                } else if let Some(r) = lt.strip_prefix("B1:") {
                    ('1', r)
                } else if let Some(r) = lt.strip_prefix("B2:") {
                    ('2', r)
                } else if let Some(r) = lt.strip_prefix("B0:") {
                    ('0', r)
                } else {
                    ('-', lt)
                };
                match kind {
                    'L' | 'R' | 'J' => {
                        let (a, b) = rest.split_once("->").unwrap_or((rest, ""));
                        if let (Some(&ia), Some(&ib)) = (name_trim.get(a.trim()), name_trim.get(b.trim())) {
                            let v = match kind {
                                'L' => &mut config.pl,
                                'R' => &mut config.pr,
                                _ => &mut config.pj,
                            };
                            v[ia] = ib as St;
                        } else {
                            eprintln!("rule file: unknown state in projection line: {}", lt);
                        }
                    }
                    '1' | '2' | '0' => {
                        let (lhs, rhs) = rest.split_once("->").unwrap_or((rest, ""));
                        let names: Vec<&str> = lhs.split("##").map(|x| x.trim()).collect();
                        let idx: Option<Vec<usize>> =
                            names.iter().map(|x| name_trim.get(*x).copied()).collect();
                        match (idx, name_trim.get(rhs.trim())) {
                            (Some(v), Some(&t)) if v.len() == 3 => {
                                let flags: u8 = match kind { '1' => 1, '2' => 2, _ => 0 };
                                config.bmap.insert((flags, v[0] as St, v[1] as St, v[2] as St), t as St);
                            }
                            _ => eprintln!("rule file: bad boundary rule line: {}", lt),
                        }
                    }
                    _ => {
                        // 拡張状態(索引 >= 2^BITS、純粋シフト線モードの境界専用状態)を
                        // 含む通常行は密な表に入らないので疎な表(フラグ不問)へ回す。
                        let (lhs, rhs) = lt.split_once("->").unwrap_or((lt, ""));
                        let names: Vec<&str> = lhs.split("##").map(|x| x.trim()).collect();
                        let idx: Option<Vec<usize>> =
                            names.iter().map(|x| name_trim.get(*x).copied()).collect();
                        let big = idx.as_ref().map_or(false, |v| v.iter().any(|&i| i >= (1 << BITS)));
                        if big {
                            if let (Some(v), Some(&t)) = (idx, name_trim.get(rhs.trim())) {
                                config.bmap.insert((0, v[0] as St, v[1] as St, v[2] as St), t as St);
                            }
                        } else {
                            let (_, i, j) = parse_rules(&line, n, &name_idx);
                            config.rules[i] = j as St;
                            config.defined[i / 64] |= 1u64 << (i % 64);
                        }
                    }
                }
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

fn fired(cells: &[St], firing: usize) -> bool {
    for i in 1..cells.len() - 1 {
        if cells[i] as usize != firing {
            return false;
        }
    }
    true
}

fn new_line(n: usize, config: &Config) -> Vec<St> {
    let mut cells: Vec<St> = vec![config.soldier as St; n + 2];
    cells[0] = config.external as St;
    cells[n + 1] = config.external as St;
    cells
}

fn first_line(n: usize, config: &Config) -> Vec<St> {
    let mut cells = new_line(n, config);
    cells[1] = config.general as St;
    cells
}

// ruby の Simulator#dump と同じ書式(状態名を | でつなぐ、色なし)。
// -d モードで ruby と diff 比較できるようにするため。
fn dump_plain(cells: &[St], config: &Config) {
    let names: Vec<&str> = cells[1..cells.len() - 1]
        .iter()
        .map(|&c| config.states[c as usize].name.as_str())
        .collect();
    println!("{}", names.join("|"));
}

// dump 行を書き出す。color なら dumpln(ルールファイルの前/背景色を
// 使った ANSI 出力、1〜発火ステップまでの実セルのみ)を用いる。
fn dump_line(cells: &[St], config: &Config, color: bool) {
    if color {
        dumpln(cells, config);
        return;
    }
    dump_plain(cells, config);
}

// ── 時空図の記録(--html / --json)──────────────────────────────────────
// -d/-c が端末に出す内容(各時刻の状態行、故障区間の R:/L: 線の内容、F: 凍結行、
// sig: 注記)をそのまま構造として持ち、ブラウザで見る自己完結 HTML(viewer.html に
// JSON を埋め込む)か JSON に書き出す。行は実セル 1..=n のみ。
struct Trace {
    rows: Vec<Vec<St>>,        // rows[t][c-1] = 時刻 t のセル c の状態
    to_right: Vec<Vec<St>>,    // 故障セルの右行き線の内容(正常セルは 0 で無視)
    to_left: Vec<Vec<St>>,     // 故障セルの左行き線の内容
    frozen: Vec<Vec<bool>>,    // F: 行相当(凍結フラグまたは凍結変状態)
    notes: Vec<(usize, String)>, // (時刻, 注記): sig: 行
    header: Vec<String>,       // a1: 行などの前置き注記
}

impl Trace {
    fn new() -> Trace {
        Trace { rows: vec![], to_right: vec![], to_left: vec![], frozen: vec![], notes: vec![], header: vec![] }
    }

    fn push(&mut self, current: &[St], ff: Option<&FaultField>, config: &Config) {
        let n = current.len() - 2;
        self.rows.push(current[1..=n].to_vec());
        match ff {
            Some(ff) => {
                self.to_right.push(ff.to_right[1..=n].to_vec());
                self.to_left.push(ff.to_left[1..=n].to_vec());
                let fr: Vec<bool> = (1..=n).map(|c| ff.frozen[c] || ff.rule_frozen(c, current, config)).collect();
                self.frozen.push(fr);
            }
            None => {
                self.to_right.push(vec![0; n]);
                self.to_left.push(vec![0; n]);
                self.frozen.push(vec![false; n]);
            }
        }
    }
}

// 規則ファイルの色は BBGGRR の 16 進(color_code と同じ解釈)。CSS の #rrggbb にする。
fn css_color(color: &str) -> String {
    if color.len() < 6 {
        return "#808080".to_string();
    }
    format!("#{}{}{}", &color[4..6], &color[2..4], &color[0..2])
}

fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '<' => out.push_str("\\u003c"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn json_rows<T: Copy>(rows: &[Vec<T>], f: impl Fn(T) -> String) -> String {
    let mut out = String::from("[");
    for (i, row) in rows.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push('[');
        for (j, &v) in row.iter().enumerate() {
            if j > 0 {
                out.push(',');
            }
            out.push_str(&f(v));
        }
        out.push(']');
    }
    out.push(']');
    out
}

// 時空図の JSON。meta は {n, faults, a3, pure, result, rule, cmd}、
// states は規則ファイルの全状態(名前・前景・背景・種別)、pure のときは射影表も。
fn trace_json(trace: &Trace, config: &Config, mask: &[bool], meta: &[(&str, String)], pure: bool) -> String {
    let mut out = String::from("{");
    out.push_str("\"meta\":{");
    for (i, (k, v)) in meta.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&format!("{}:{}", json_str(k), v));
    }
    out.push_str("},\"states\":[");
    for (i, st) in config.states.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&format!(
            "{{\"n\":{},\"fg\":{},\"bg\":{},\"k\":{}}}",
            json_str(st.name.trim()),
            json_str(&css_color(&st.foreground)),
            json_str(&css_color(&st.background)),
            json_str(st.class.trim())
        ));
    }
    let n = mask.len().saturating_sub(2);
    let mask_json: Vec<String> = (1..=n).map(|c| if mask[c] { "1".into() } else { "0".into() }).collect();
    out.push_str(&format!("],\"mask\":[{}]", mask_json.join(",")));
    out.push_str(&format!(",\"rows\":{}", json_rows(&trace.rows, |v: St| v.to_string())));
    out.push_str(&format!(",\"toRight\":{}", json_rows(&trace.to_right, |v: St| v.to_string())));
    out.push_str(&format!(",\"toLeft\":{}", json_rows(&trace.to_left, |v: St| v.to_string())));
    out.push_str(&format!(",\"frozen\":{}", json_rows(&trace.frozen, |v: bool| if v { "1".into() } else { "0".into() })));
    out.push_str(",\"notes\":[");
    for (i, (t, text)) in trace.notes.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&format!("{{\"t\":{},\"text\":{}}}", t, json_str(text)));
    }
    out.push_str("],\"header\":[");
    for (i, text) in trace.header.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push_str(&json_str(text));
    }
    out.push(']');
    if pure {
        let arr = |v: &Vec<St>| -> String {
            let xs: Vec<String> = v.iter().map(|x| x.to_string()).collect();
            format!("[{}]", xs.join(","))
        };
        out.push_str(&format!(",\"pl\":{},\"pr\":{},\"pj\":{}", arr(&config.pl), arr(&config.pr), arr(&config.pj)));
    }
    out.push('}');
    out
}

const VIEWER_HTML: &str = include_str!("../viewer.html");

// --html / --json の書き出し。HTML は viewer.html の `/*__FSSP_DATA__*/null` を
// JSON で置き換えた自己完結ファイル。
fn write_trace_outputs(
    html: &Option<String>,
    json: &Option<String>,
    trace: &Trace,
    config: &Config,
    mask: &[bool],
    meta: &[(&str, String)],
    pure: bool,
) {
    if html.is_none() && json.is_none() {
        return;
    }
    let data = trace_json(trace, config, mask, meta, pure);
    if let Some(path) = json {
        if let Err(e) = std::fs::write(path, &data) {
            eprintln!("--json: cannot write {}: {}", path, e);
        } else {
            println!("json: {}", path);
        }
    }
    if let Some(path) = html {
        let marker = "/*__FSSP_DATA__*/null";
        if !VIEWER_HTML.contains(marker) {
            eprintln!("--html: viewer template lacks the data marker");
            return;
        }
        let page = VIEWER_HTML.replacen(marker, &data, 1);
        if let Err(e) = std::fs::write(path, page) {
            eprintln!("--html: cannot write {}: {}", path, e);
        } else {
            println!("html: {}", path);
        }
    }
}

// -d モード用: 逐次で 1 ステップごとに途中経過を出力する。
// 出力は「初期配置 + 1〜発火ステップまでの各行」で ruby -d と同じ列になる。
// (並列ではワーカー間のトークン同期の中で全体を一つのタイミングで
//  参照できる地点がないため、ダンプモードは逐次実行にしている)
fn simulate_dump(
    current: &mut Vec<St>,
    next_cells: &mut Vec<St>,
    config: &Config,
    n: usize,
    print: bool,
    color: bool,
    mut trace: Option<&mut Trace>,
) -> usize {
    if print {
        dump_line(current, config, color);
    }
    if let Some(tr) = trace.as_deref_mut() {
        tr.push(current, None, config);
    }
    let mut t = 0;
    while !fired(current, config.firing) && t <= ((n << 1) - 2) {
        nextline(current, next_cells, config);
        t += 1;
        std::mem::swap(current, next_cells);
        if print {
            dump_line(current, config, color);
        }
        if let Some(tr) = trace.as_deref_mut() {
            tr.push(current, None, config);
        }
    }
    t
}

fn simulate<'a>(
    mut current: &'a mut [St],
    mut next_cells: &'a mut [St],
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
    cur: &'a mut [St],
    nex: &'a mut [St],
    sender: [Option<Sender<i32>>; 2],
    receiver: [Option<Receiver<i32>>; 2],
}

fn split<'a>(ary1: &'a mut [St], ary2: &'a mut [St], n: isize) -> Vec<AryPair<'a>> {
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
    cur: &'a mut [St],
    nex: &'a mut [St],
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
// 用語(2026-11-06 改名。旧称: 管路 / 入口 / 遠位口):
//   - 故障区間 D_i = I_i と I_{i+1} に挟まれた故障セルの並び。信号だけを運ぶ
//     2 本の一方向線(右行き線 to_right、左行き線 to_left)として扱う。
//   - D_i 左端 = D_i の左端セル(左隣が I_i の右端)。右行き線はここで I_i の
//     担体(CB/NG)を拾い、左行き線はここで I_i の右端に FT / a/b 波を提示する。
//   - D_i 右端 = D_i の右端セル(右隣が I_{i+1} の左端)。右行き線はここで
//     I_{i+1} に提示し、左行き線はここで I_{i+1} 側の帰還波(T3/bR/cBT)を拾う。
//   - 始端 / 終端 = 流れる向きに対して信号が乗る側 / 提示される側の端
//     (右行き線: 左端→右端、左行き線: 右端→左端)。注入の説明にだけ使う。
//   - 信号名は論文 Fig.6 に揃える(旧称: fast 波 / journey):
//     1/1 信号 = 将軍が t=0 に出す速度 1/1 の信号(I_i 内では Balzer の C 波、
//       I_i 右端で担体 CB、未起床区間では通過 TR)。各区間右端で反射を生む。
//     1/3 信号 = 将軍が t=0 に出す速度 1/3 の信号(位相 S33→S3A→S3B を宿主に
//       重ねて表示: MS33、CS3A、FmS3B)。第 2 反射と会合して NG を出す。
//     第 1 反射 = I_i 右端の凍結起動(FS 相当)、第 2 反射 = R2(線内は cBT)、
//     第 3 反射 = T3(配列右端の壁エコー、解凍)。状態名 R2/T3/S3x は不変。
// 信号層(A1 モード = -f 指定かつ --inject/--wallset なし):
//   - 故障区間は規則層で実装した凍結波/解凍波(balzer-freeze.rul.txt の F 状態)への
//     起動信号だけを運ぶ。信号は A1/A2 の式(t_i, t_i1, Δt_i)から区間構造より
//     事前計算し、故障区間の始端(信号が乗る側の端)から注入して 1/1 で流れ、終端のルール入力に
//     ちょうど 1 ステップだけ提示される(壁の役割と同じ提示機構)。
//   - 故障区間が信号を運んでいないときは www を提示する(区間の境界は、区間内の
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

// A1/A2 のスケジュールに従い故障区間へ注入される信号。
// time = 故障区間の始端セルに値が現れる時刻、cell = 始端セル(故障セル)。
// to_left = true なら左行き線(to_left、D_i 右端から注入)、false なら右行き線(to_right、
// D_i 左端から注入)。信号は故障区間を 1/1 で流れ、終端で 1 ステップだけ提示されて消える。
struct PlannedSignal {
    time: usize,
    cell: usize,
    to_left: bool,
    value: St,
}

fn find_state_index(config: &Config, name: &str) -> Option<St> {
    config
        .states
        .iter()
        .position(|s| s.name.trim() == name)
        .map(|i| i as St)
}

// Umeo §3 (A1/Theorem 2, A2/Theorem 3) の式から信号スケジュールを組み立てる。
//  正常区間 I_i (i = 1..p+1) の将軍起動:  t_i  = 2 Σ_{j<i} (n_j + m_j)
//  I_i の凍結起動:     t_i1 = t_i + n_i - 1 (i ≤ p、A1 本文の t1 = n1 - 1 と一致)
//  I_i の凍結期間:     Δt_i = 2 m_i + 2 Σ_{j=i+1..p} (n_j + m_j) + 2 n_{p+1}
//  I_i の解凍起動:     t_i2 = t_i1 + Δt_i
// 信号は対応する故障区間 D に流し込まれ、提示時刻(起動時刻の 1 ステップ前)
// に終端のルール入力へ現れる。
fn plan_a1_signals(analysis: &FaultAnalysis, config: &Config, a3: bool) -> (Vec<PlannedSignal>, Vec<String>) {
    let mut plan = vec![];
    let mut notes = vec![];
    let p = analysis.segments.iter().filter(|s| !s.intact).count();
    let fs = find_state_index(config, "FS");
    let ft = find_state_index(config, "FT");
    let ng = find_state_index(config, "NG");
    // A3 ②-C: ルールファイルに会合状態 abX があれば解凍は a/b 会合機構
    // (規則層+故障セル処理(rust 側))で担うので、A3 モードの FT 注入を行わない。
    let a3_ab = a3 && find_state_index(config, "abX").is_some();

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
        // 解凍信号 FT: p=1 では注入しない(壁反射の T3 波の D_i 右端 pickup が
        // 純 FT に変換して同じ時刻に提示する=FT 注入を撤廃した p=1 経路)。
        // p>=2 では区間識別つき往復波が未実装のため、各 D_i への隔離配信(注入)
        // で解凍提示する(論文に対する既知の置換装置の記録: SPEC「実装の記録」)。
        let t_1i = t_i(i) + n_i - 1;
        let t_2i = t_1i + delta;
        // p=1 の解凍は 1/1 信号の壁反射 T3 を D1 右端が FT に変換する(局所)。
        // n_1=1 でも将軍の初期表示を D_i 左端が担体として拾うので同じ経路。
        let local_thaw = p == 1;
        // ②-E: I1 が 1 セル(将軍自身が右端)でも、D_i 左端が初回 pickup の M を
        // 担体として拾い(1/1 信号の原点)、(www,M,W)→MS33 で凍結+1/3 信号 を
        // 始めるので FS/FT 注入は不要。
        let a3_local = a3_ab;
        if let Some(ft) = ft {
            if n_i >= 2 {
                notes.push(format!(
                    "I{} frozen Δt={} (freeze via boundary reflection at t={}, {} at t={})",
                    i,
                    delta,
                    t_1i,
                    if a3_local { "a/b meeting abX (rule layer, no injection)" } else { "FT" },
                    t_2i - 1
                ));
            } else {
                notes.push(if a3_local {
                    format!(
                        "I{} frozen Δt={} (1-cell: CB shown on NG arrival at t={}, a/b meeting abX at t={})",
                        i, delta, t_1i, t_2i - 1
                    )
                } else {
                    format!(
                        "I{} frozen Δt={} (1-cell: freeze at t={}, {} at t={})",
                        i,
                        delta,
                        t_1i,
                        if local_thaw { "T3 echo thaw (rule layer)" } else { "FT" },
                        t_2i - 1
                    )
                });
            }
            // n_i = 1 の縮退区間だけ FS を残す(C 波が存在しない: i=1 では t_1i =
            // 0 をクランプ、i>=2 では t_1i = t_i なので通常の 1 ステップ前)。
            // A3 ②-C: i>=2 の 1 セル区間は起動 NG 到着時の表示が担体 CB
            // ((NG,L,W)→CB、局所)なので FS も FT も注入しない。I1 が 1 セル
            // (将軍自身が右端、t_11=0)だけは FS/FT 注入を残す(縮退)。
            // ②-F: FS 注入は全モードで撤廃。1 セル区間は i>=2 なら (NG,L,W)→CB、
            // I1 なら (www,M,W)→MS33(将軍の初期表示を D_i 左端が担体として拾う)。
            let _ = fs;
            if !local_thaw && !a3_local {
                plan.push(PlannedSignal {
                    time: t_2i - 1,
                    cell: d.start,
                    to_left: true,
                    value: ft,
                });
            }
        }
        // 次の正常区間 I_{i+1} の将軍起動信号(右行き線を流れて
        // I_{i+1} の左端セルの左入力に提示される)。p=1 では全配置が会合機構
        // (CA 局所)で扱えるとは限らない(dt <= S では放出波が解凍前線に吸収
        // されるので注入での代替を残す=後段課題)。
        // p>=2 でも I_i 対 D_i 対 I_{i+1} は p=1 と同じ対面幾何なので、
        // 同じ会合機構(規則層の規則は時刻に依存しない=そのまま再利用)で
        // 処理できる。配信可能条件は p=1 のもののペア版:
        //   - 会合セル c_m <= n_i(故障区間への装填は S3A+TR 同時放出キーで c=n1 でも可)
        //   - dt > S_i で起動波が解凍前線に吸収されない
        //   - n_{i+1} != 1(n_{i+1}=1 では壁反射が R2 装填のポート提示を奪う)
        if let Some(ng) = ng {
            let s1 = n_i + m_i;
            let c_meet = if s1 % 2 == 0 { s1 / 2 + 1 } else { (s1 + 3) / 2 };
            let n_next = sizes[2 * i];
            // A3 (Thm.4) は時刻系に +(i−1) のオフセットを足すため会合放出の
            // 配信時刻が 1 ステップ早くなる。会合機構は A1/A2 の時刻系なの
            // で、A3 モードでは注入のまま(残課題: A3 対応の放出キー)。
            // ②-B per-pipe 化済み: CB/NG D_i 左端 pickup と D_i 右端(to_left)の TR
            // 折り返しは各 D_i で行う(rust 側 own_wave ゲート)。対 (I_i,I_{i+1})
            // が配信可能なら I_i の会合機構(自身の 1/3 信号 は add_birth の
            // 誕生+next_pair_deliverable アームで走る)が放出 NG で I_{i+1} を
            // 起動する。A3 は時刻系がずれるので注入のまま。
            // A3 ②-D: 会合機構は A3 でも同じ幾何で動く。+1 は放出 NG が D_i 左端の
            // 1 歩遅延線(b 波の前進停留と同じ仕組み)を通ることで出る(論文: 「各正常
            // 区間で起動が 1 歩遅れる」。機構は論文に指定なし)。
            // ②-E: 会合セルが D_i 左端(c_meet = n_i+1、m_i ∈ {n_i−1, n_i})に落ちる
            // 対は、D_i 左端セルが会合(1/3 信号の位相 × cBT 到着)を行い NG を
            // 生成する。c_meet <= n_i の条件は不要になり、n_i >= 2(1/3 信号 が
            // 存在する)だけ残る。
            let _ = c_meet;
            // A3: 1 セル区間も 1/3 信号 を持つ((L|NG,CB,W)→LS33、(www,M,W)→MS33)
            // ので n_i / n_{i+1} の縮退条件は不要。残るのは dt <= S(放出 NG と
            // 解凍前線の正面衝突)だけ。
            // ②-F: 放出 NG は解凍前線と交差して起床済みセル上を xNG で走るので
            // dt <= S の条件も不要。A3 は全対が会合機構で配信される。
            let pair_deliverable = true;
            let s1_last = sizes[sizes.len() - 1];
            let deliverable = 2 * m_i + 2 * s1_last > s1;
            let _ = (deliverable, s1_last);
            if !pair_deliverable {
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
    notes.push(format!("injected signals = {}", plan.len()));
    (plan, notes)
}

struct FaultField {
    mask: Vec<bool>,      // 実セル 1..=n が true。0 と n+1(壁)は false
    to_right: Vec<St>,    // L→R ストリーム: c が t に保持する「左に由来する値」
    to_left: Vec<St>,     // R→L ストリーム: c が t に保持する「右に由来する値」
    ctrl: Vec<Ctrl>,      // 制御メッセージチャネル(1..=n)
    frozen: Vec<bool>,    // 凍結フラグ(正常セルのみ効果あり)
    to_right_nex: Vec<St>,
    to_left_nex: Vec<St>,
    ctrl_nex: Vec<Ctrl>,
    // A1 モード: 故障区間は信号だけを運び、アイドル時は境界マーカーを提示する
    signal_only: bool,
    // 信号状態の番号。ルールファイルに無ければ None(www にフォールバック)
    w_state: Option<St>,  // 左行き線のアイドル提示(解凍前の境界マーカー)
    ft_state: Option<St>, // 解凍信号
    ft2_state: Option<St>, // 解凍後のアイドル提示(σ 線上の壁反射と凍結起動の区別)
    cb_state: Option<St>,  // 境界 Carrier(1/1 信号の故障区間 entry pickup 対象)
    cbt_state: Option<St>, // 担体横断の専用表示(R2 装填の D_i 右端 pickup 対象)
    tr_state: Option<St>,  // 1/1 信号の継続の対岸横断状態
    a3: bool,              // A3 時刻系(会合機構の規則を凍結する)
    // 壁反射の左走行波 T3 と、起床済みセル上の複合 carrier <X>T3(chateau+signal)。
    // D_i 右端 pickup の対象。pickup した複合は純 FT に変換して故障区間に乗せる。
    t3_carriers: Vec<St>,
    // NG 会合放出の保値付き carrier {X}NG(名称が 3 文字で NG で終わる)。
    // 会合セルから 1/1 右走行して I1 右端に現れたとき、D_i 左端(to_right) pickup
    // (左端セル d1.0 = D1 左端だけ)がこれを故障区間に乗せる。
    ng_carriers: Vec<St>,
    // NG 状態(注入始発の素値)。2 歩提示の判定に使う。
    ng_state: Option<St>,
    // NG 起動中継の 2 歩提示(D_i 右端で 1 歩保持)。
    ng_hold: Vec<St>,
    ng_hold_nex: Vec<St>,
    // 故障区間数。左行き線(to_left)への FT 変換 pickup は p=1 のときだけ有効
    // (p>=2 では各区間の解凍は注入 FT で提示され、壁反射波を区間に拾わせると
    //  他区間の波が誤って解凍を起動するため、波は故障区間の右端で消える)
    p: usize,
    // D1 の(左端セル, 右端セル)。to_left の FT 変換 pickup は p=1 のとき
    // d1.1 で有効(②-C までの制約)。
    // None = 旧挙動(全故障区間で pickup、テスト用)。
    d1: Option<(usize, usize)>,
    // 全故障区間の(左端セル, 右端セル)リスト。②-B: CB/NG の D_i 左端 pickup と
    // D_i 右端(to_left)の TR 折り返し(R2 装填)は D1 に限らず各 D_i で行う。
    pipes: Vec<(usize, usize)>,
    // D_i ごとに「最後に 右行き線の左端 pickup した値が自身の担体(CB)か」。
    // I_i の担体(凍結 Carrier)はこの故障区間に入ったばかり=R2 折り返しの源。
    // 1/1 信号(I1 担体)は横断で TR として D_i 左端に現れるので false=折り返し禁止。
    // これがないと 1/1 信号の折り返しが各 I_i の凍結起動時刻をずらして
    // (C,L,W)→FL の W スロットを壊す。
    own_wave: Vec<bool>,
    own_wave_nex: Vec<bool>,
    // A3 ②-C: CB 入庫の 1 ステップ後に流す 2 便目(a 波 aF)。one-shot。
    ab_hold: Vec<St>,
    ab_hold_nex: Vec<St>,
    // D_i 両端の 1 歩遅延線: D_i 左端 to_right(b 前進の停留)、D_i 左端 to_left(a 帰還の停留)
    r_buf: Vec<St>,
    r_buf_nex: Vec<St>,
    l_buf: Vec<St>,
    l_buf_nex: Vec<St>,
    // 会合済みの左端(abX 提示後、後続の a 脚を呑む)
    ab_consume: Vec<bool>,
    ab_consume_nex: Vec<bool>,
    // 帰還 carrier 族(bR で終わる状態名)、前進/帰還の射影(状態番号→成分)
    br_carriers: Vec<St>,
    fw_proj: Vec<Option<St>>,
    bw_proj: Vec<Option<St>>,
    bf_state: Option<St>,
    af_state: Option<St>,
    abx_state: Option<St>,
    t3_state: Option<St>,
    br_state: Option<St>,
    wt3_state: Option<St>,
    wbr_state: Option<St>,
    // ②-D: D_i 右端の折り返し cBT と帰還波の同時提示(故障区間の提示値の複合)
    cbt_t3_state: Option<St>,
    cbt_br_state: Option<St>,
    // ②-G: 折り返し cBT と解凍 FT の同時提示(A1、I_{p+1} が 1 セル)
    cbt_ft_state: Option<St>,
    // ②-H: 自分の D_i の a 波の D_i 内マーカー(aFo。提示時に aF へ射影)と、D_i 右端が
    // 「前ステップに自分の担体(CB / aFo)を提示した」1 歩記憶。折り返しの判定は
    // これで行う(sticky な own_wave では m_i >= n_i のとき前段の a 波の表示を
    // 自分の a と誤認して偽の折り返しが起きた: 実測 A3 n=9 -f 2,6-8)。
    afo_state: Option<St>,
    own_pres: Vec<bool>,
    own_pres_nex: Vec<bool>,
    // (a) 純粋シフト線モード: 故障セルは生の状態を写すだけ。境界セルは Config の
    // 境界表(bmap)と射影(PL/PR)で規則を引く。他の故障区間処理は全て無効。
    pure: bool,
    // ②-E: 凍結 1/3 信号 の位相集合(複合 ⊕bw 込み)。 D_i 左端の会合判定に使う。
    j_s33_frozen: Vec<St>,
    j_s3a_frozen: Vec<St>,
    // ②-E: 故障区間がまだ何も運んでいない(初期状態)。初回 pickup で左隣が M
    // (= 1 セル I1 の将軍の初期表示)なら担体として拾う(1/1 信号の原点)。
    pipe_virgin: Vec<bool>,
    m_state: Option<St>,
    // ②-E 重ね合わせ: 前進対 aF⊕bF(名前が aFbF で始まる)、帰還対 T3⊕bR
    // (名前が T3bR で終わる)、D_i 提示の cBT⊕T3⊕bR。
    is_abf: Vec<bool>,
    is_t3br: Vec<bool>,
    abf_state: Option<St>,
    t3br_state: Option<St>,
    cbt_t3br_state: Option<St>,
    trace_fb: bool,
}

impl FaultField {
    fn intact(&self, c: usize) -> bool {
        !self.mask[c]
    }

    // 値 v が NG 起動中継の 2 歩提示の対象か(会合放出の保値付き carrier
    // {X}NG、または注入始発の素の NG 状態)。
    fn is_ng_launch(&self, v: St) -> bool {
        self.ng_carriers.contains(&v) || self.ng_state == Some(v)
    }

    // D_i 右端 c から右の区間が I_{i+1} として、対 (I_{i+1}, I_{i+2}) の会合
    // 対が配信可能か(plan_a1_signals の pair_deliverable と同じ条件)。
    // 配信可能な対だけが自分の 1/3 信号(S33 誕生)を持つ意味がある:
    //   - 次の故障区間 D_{i+1} が存在する(i+1 <= p)。無ければ I_{i+1} は最後の
    //     区間 I_{p+1} で、2n-2 でそのまま発火するので 1/3 信号 不要。
    //   - c_meet(i+1) <= n_{i+1}、dt_{i+1} > S_{i+1}、n_{i+2} != 1。
    // これらが揃わない配置では 1/3 信号 の誕生が発火時刻と衝突して
    // choreography を壊す(a1m バッテリと A2 スイープで発見)ので、
    // 起動中継の 2 歩提示をアームしない=誕生キー (NG|LNG,M,L) が生じない。
    // D_i 右端 c の D_i とその左右の区間で対 (I_i, I_{i+1}) が配信可能か
    // (plan_a1_signals の pair_deliverable と同じ条件)。配信できない対の
    // 故障区間では自身の担体(CB)を通さない(放出も注入で代替。担体の 1 歩 TR が
    // 1/1 信号の壁反射 T3 と衝突し、欠落キー=FB で choreography を壊すため)。
    // n_i(左区間)、m_i(この故障区間)、n_{i+1}(右区間)を mask から数える。
    fn this_pair_deliverable(&self, c: usize) -> bool {
        let n = self.mask.len() - 2;
        if c == 0 || c > n {
            return false;
        }
        // m_i は故障区間リストから取る(D1 の左には別の故障区間がないので
        // 左方向の故障走で数えると配列端まで延びて壊れる)。
        let m_i = self
            .pipes
            .iter()
            .filter(|&&(s, e)| e == c)
            .map(|&(s, _)| c - s + 1)
            .max()
            .unwrap_or(0);
        let mut x = c.saturating_sub(m_i);
        let mut n_i = 0usize;
        while x >= 1 && !self.mask[x] {
            n_i += 1;
            x -= 1;
        }
        let mut y = c + 1;
        let mut n_i1 = 0usize;
        while y <= n && !self.mask[y] {
            n_i1 += 1;
            y += 1;
        }
        if n_i == 0 || n_i1 == 0 {
            return false;
        }
        let s = n_i + m_i;
        let c_meet = if s % 2 == 0 { s / 2 + 1 } else { (s + 3) / 2 };
        let _ = (c_meet, s, n_i, n_i1);
        true
    }

    fn next_pair_deliverable(&self, c: usize) -> bool {
        let n = self.mask.len() - 2;
        let mut x = c + 1;
        if x > n {
            return false;
        }
        // 実験(規則層化の可否判断): FSSP_NPD_ALWAYS=1 で「次の対が存在する」を常に
        // 真にし、最終区間でも 1/3 信号 が生まれる(=非局所な最終区間知識を使わない)
        // 場合の挙動を実測する。
        if std::env::var("FSSP_NPD_ALWAYS").is_ok() {
            return true;
        }
        let mut n_i1 = 0usize;
        while x <= n && !self.mask[x] {
            n_i1 += 1;
            x += 1;
        }
        let mut m_i1 = 0usize;
        while x <= n && self.mask[x] {
            m_i1 += 1;
            x += 1;
        }
        let mut n_i2 = 0usize;
        while x <= n && !self.mask[x] {
            n_i2 += 1;
            x += 1;
        }
        if m_i1 == 0 || n_i1 == 0 || n_i2 == 0 {
            return false; // 最後の区間 or 変則配置: 1/3 信号 不要
        }
        let s = n_i1 + m_i1;
        let c_meet = if s % 2 == 0 { s / 2 + 1 } else { (s + 3) / 2 };
        let _ = (c_meet, s, n_i1, n_i2);
        true
    }

        // c がどれか故障区間の左端セル(左隣=正常)か。右隣が正常(m=1 の D_i)でも
    // D_i 左端なので mask[c+1] は見ない。
    fn is_pipe_entry(&self, c: usize) -> bool {
        c >= 1 && self.intact(c - 1)
    }

    fn is_cbt_family(&self, v: St) -> bool {
        self.cbt_state == Some(v) || self.cbt_t3_state == Some(v) || self.cbt_br_state == Some(v)
            || self.cbt_t3br_state == Some(v) || self.cbt_ft_state == Some(v)
    }

    // D_i 右端 c が対岸の表示 d_next から cBT(第 2 反射の折り返し)を取り込む
    // ステップか。A3: 自分の D_i の a 波(1/1 信号の継続)表示、非 A3: cBT 表示。
    fn fold_at_distal(&self, c: usize, d_next: St) -> bool {
        if !self.this_pair_deliverable(c) || !self.own_pres[c] {
            return false;
        }
        if self.a3 {
            self.fw_proj[d_next as usize].is_some() && self.fw_proj[d_next as usize] == self.af_state
        } else {
            // cBT 表示、または I_{i+1} が 1 セルで担体が即時壁反射した T3 表示
            // (own_wave は折り返しで落ちるので、自分の D_i の担体の反射に限られる)
            Some(d_next) == self.cbt_state || Some(d_next) == self.t3_state
        }
    }


    // c の属する故障区間のインデックス(左端セルから)。無ければ None。
    fn pipe_index_of_entry(&self, c: usize) -> Option<usize> {
        self.pipes.iter().position(|&(s, _)| s == c)
    }

    // D_i 右端 c の属する故障区間の own_wave フラグ(最後の L→R D_i 左端値が CB)。
    // フラグは左端セルのインデックスに保存してある。
    fn own_wave_of_distal(&self, c: usize) -> bool {
        self.pipes
            .iter()
            .filter(|&&(_, e)| e == c)
            .any(|&(s, _)| self.own_wave[s])
    }

    // 対岸の正常セルの表示 d が故障区間に乗せる解凍信号(FT)かどうか。
    // 純 FT に加え、壁反射の左走行波 T3 と複合 carrier <X>T3 も対象:
    // 複合は chateau 部分をセル自身に残すので、故障区間には純 FT を載せる。
    // TR は I2 左端の 1/1 信号の継続表示: R2 装填(会合の 1/1 左走行反射信号)の
    // 源流として素の TR 状態のまま 左行き線(to_left)に乗せる(終端のルール入力に
    // 1 歩だけ提示され、I1 右端の (F_l, F_v, TR) -> {v}R2 で凍結領域を左走行)。
    fn pickup_thaw(&self, d: St) -> Option<St> {
        if Some(d) == self.ft_state || self.t3_carriers.contains(&d) {
            self.ft_state
        } else if Some(d) == self.cbt_state {
            // 対の担体の横断表示: R2 装填の源として to_left にそのまま乗せる
            // ((F_l,F_v,cBT)→{v}R2 変換は規則層)
            self.cbt_state
        } else if Some(d) == self.tr_state {
            Some(d)
        } else {
            None
        }
    }

    fn new(mask: &[bool], config: &Config, signal_only: bool) -> FaultField {
        Self::new_ctx(mask, config, signal_only, 0, None, Vec::new(), false, false)
    }

    fn new_ctx(
        mask: &[bool],
        config: &Config,
        signal_only: bool,
        p: usize,
        d1: Option<(usize, usize)>,
        pipes: Vec<(usize, usize)>,
        a3: bool,
        pure: bool,
    ) -> FaultField {
        let n = mask.len() - 2;
        let w_state = find_state_index(config, "W");
        let ft_state = find_state_index(config, "FT");
        let ft2_state = find_state_index(config, "FT2");
        let cb_state = find_state_index(config, "CB");
        let cbt_state = find_state_index(config, "cBT");
        let tr_state = find_state_index(config, "TR");
        // T3 と複合 carrier <X>T3(X ∈ A,B,C,L,M,Q,R)の状態番号。名前の
        // 規約は「T3 で終わる 2〜3 文字の名前」。ルールファイルに無ければ空。
        let t3_carriers: Vec<St> = config
            .states
            .iter()
            .enumerate()
            .filter(|(_, s)| {
                let name = s.name.trim();
                // T3 / <X>T3(3 文字)に加え、1/3 信号 複合の T3 付き
                // carrier lo{X}{ph}T3(6 文字・先頭小文字)も pickup 対象
                // (T3 エコーが I_{i+1} の 1/3 信号 滞在中のセルを 1/1 で
                //  抜ける: ②-B 衝突族)。
                name == "T3"
                    || (name.len() == 3 && name.ends_with("T3"))
                    || (name.len() >= 4
                        && name.len() <= 6
                        && name.ends_with("T3"))
            })
            .map(|(i, _)| i as St)
            .collect();
        // NG 会合放出の保値付き carrier {X}NG(3 文字で NG で終わる名前)。
        let ng_carriers: Vec<St> = config
            .states
            .iter()
            .enumerate()
            .filter(|(_, s)| {
                let name = s.name.trim();
                name.len() == 3 && name.ends_with("NG")
            })
            .map(|(i, _)| i as St)
            .collect();
        let ng_state = find_state_index(config, "NG");
        // b 前進/帰還 carrier(bF / bR)と会合提示(abX)。
        // 帰還族は「bR で終わる 2〜6 文字の状態名」(純 bR + 複合 <X>bR 等)。
        let bf_state = find_state_index(config, "bF");
        let af_state = find_state_index(config, "aF");
        let abx_state = find_state_index(config, "abX");
        let t3_state = find_state_index(config, "T3");
        let br_state = find_state_index(config, "bR");
        let wt3_state = find_state_index(config, "WT3");
        let wbr_state = find_state_index(config, "WbR");
        let cbt_t3_state = find_state_index(config, "cBTT3");
        let cbt_br_state = find_state_index(config, "cBTbR");
        let cbt_ft_state = find_state_index(config, "cBTFT");
        let afo_state = find_state_index(config, "aFo");
        let frozen_phase = |ph: &str| -> Vec<St> {
            config
                .states
                .iter()
                .enumerate()
                .filter(|(_, s)| {
                    let name = s.name.trim();
                    let base = if name.ends_with("T3") || name.ends_with("bR") {
                        &name[..name.len() - 2]
                    } else {
                        name
                    };
                    base.len() == 4
                        && base.ends_with(ph)
                        && base.chars().next().map_or(false, |ch| ch.is_ascii_uppercase())
                })
                .map(|(i, _)| i as St)
                .collect()
        };
        let j_s33_frozen = frozen_phase("S33");
        let j_s3a_frozen = frozen_phase("S3A");
        let is_abf: Vec<bool> = config
            .states
            .iter()
            .map(|s| s.name.trim().starts_with("aFbF"))
            .collect();
        let is_t3br: Vec<bool> = config
            .states
            .iter()
            .map(|s| s.name.trim().ends_with("T3bR"))
            .collect();
        let abf_state = find_state_index(config, "aFbF");
        let t3br_state = find_state_index(config, "T3bR");
        let cbt_t3br_state = find_state_index(config, "cBTT3bR");
        let ng_base: Vec<(String, St)> = config
            .states
            .iter()
            .enumerate()
            .filter(|(_, s)| {
                let name = s.name.trim();
                name.len() == 3 && name.ends_with("NG")
            })
            .map(|(i, s)| (s.name.trim().to_string(), i as St))
            .collect();
        let br_carriers: Vec<St> = config
            .states
            .iter()
            .enumerate()
            .filter(|(_, s)| {
                let name = s.name.trim();
                name.len() >= 2 && name.ends_with("bR") && name != "abX"
            })
            .map(|(i, _)| i as St)
            .collect();
        // 状態名の射影(A3 ②-C): 前進成分(bF/aF/CB/TR/cBT とその複合の接頭部)、
        // 帰還成分(T3/bR で終わる名前)。 D_i の両端は「線に入る成分」だけを運ぶ。
        let fw_proj: Vec<Option<St>> = config
            .states
            .iter()
            .map(|s| {
                let name = s.name.trim();
                let base = if name.ends_with("T3bR") {
                    &name[..name.len() - 4]
                } else if name.ends_with("T3") || name.ends_with("bR") {
                    &name[..name.len() - 2]
                } else {
                    name
                };
                match base {
                    "bF" => bf_state,
                    "aF" | "aFbF" => af_state,
                    "CB" => cb_state,
                    "TR" => tr_state,
                    "cBT" => cbt_state,
                    // ②-D/②-F: 会合放出の担体 {X}NG / xNG(と帰還波との複合)は純 NG に
                    // 射影する(故障区間は信号だけを運ぶ。I_{i+1} 左端の起動鍵は純 NG)。
                    _ => ng_base.iter().find(|(nm, _)| nm == base).and_then(|_| find_state_index(config, "NG")),
                }
            })
            .collect();
        let bw_proj: Vec<Option<St>> = config
            .states
            .iter()
            .map(|s| {
                let name = s.name.trim();
                if name.ends_with("T3bR") {
                    find_state_index(config, "T3bR")
                } else if name.ends_with("T3") {
                    t3_state
                } else if name.ends_with("bR") && name != "abX" {
                    br_state
                } else {
                    None
                }
            })
            .collect();
        // 信号層のアイドル値。左行き線(I_i 右端セルの右入力)は境界マーカー W
        // を提示する(www だと凍結起動と素の壁反射 (C,L,www)->M が区別できず衝突)。
        // 解凍信号 FT が口を通過した後は FT2(解凍後位相)に切り替わる。
        // 右行き線(次の区間 I_{i+1} 左端セルの左入力)は www(将軍の boundary
        // 規則 (www,M,*) がそのまま働く)。ルールファイルに W がなければ www。
        // 右行き線のアイドル提示: 素の www(将軍の boundary 規則 (www,M,*) が
        // そのまま働く=以前の挙動)から L に変更。S33 排出 (www,M,L)->複合 が
        // I_{i+1} 左端の発射後 M 表示でも誤発火するため、故障区間からの左入力は
        // 細胞 1 と区別できる L を提示する((L,M,r) ミラー規則を生成する)。
        let idle_to_right = if signal_only { config.soldier as St } else { config.external as St };
        let idle_to_left = if signal_only { w_state.unwrap_or(config.external as St) } else { config.soldier as St };
        // 純粋シフト線の初期内容は静止状態 L(右端の右入力は PR(L)=W、左端の左入力は PL(L)=L)
        let idle_to_left = if pure { config.soldier as St } else { idle_to_left };
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
            cbt_state,
            tr_state,
            t3_carriers,
            ng_carriers,
            ng_state,
            ng_hold: vec![0 as St; n + 2],
            ng_hold_nex: vec![0 as St; n + 2],
            ab_hold: vec![0 as St; n + 2],
            ab_hold_nex: vec![0 as St; n + 2],
            r_buf: vec![0 as St; n + 2],
            r_buf_nex: vec![0 as St; n + 2],
            l_buf: vec![0 as St; n + 2],
            l_buf_nex: vec![0 as St; n + 2],
            ab_consume: vec![false; n + 2],
            ab_consume_nex: vec![false; n + 2],
            pipes,
            own_wave: vec![false; n + 2],
            own_wave_nex: vec![false; n + 2],
            br_carriers,
            fw_proj,
            bw_proj,
            bf_state,
            af_state,
            abx_state,
            t3_state,
            br_state,
            wt3_state,
            wbr_state,
            cbt_t3_state,
            cbt_br_state,
            cbt_ft_state,
            afo_state,
            own_pres: vec![false; n + 2],
            own_pres_nex: vec![false; n + 2],
            pure,
            j_s33_frozen,
            j_s3a_frozen,
            pipe_virgin: vec![true; n + 2],
            m_state: find_state_index(config, "M"),
            is_abf,
            is_t3br,
            abf_state,
            t3br_state,
            cbt_t3br_state,
            trace_fb: std::env::var("FSSP_TRACE_FB").is_ok(),
            p,
            d1,
            a3,
        };
        // 壁は故障ではない
        field.mask[0] = false;
        field.mask[n + 1] = false;
        field
    }

    // 1b) 会合判定(冪等: 判定後は l_buf が 0 なので再呼び出しは何もしない)。
    //     ダンプの直前にも呼び、提示値 abX をダンプに反映する。
    fn premerge(&mut self, current: &[St]) {
        let n = self.mask.len() - 2;
        // 1b) A3 ②-C 会合: D_i 左端に a 帰還(T3 族)が到着した(遅延線に入った)
        //     ステップに、その上流(1 セル右)に b 帰還(bR 族)が続いていれば自対の
        //     故障区間である(a は b の 1 歩前を走る。他の D_j では間隔 >= 2)。
        //     このとき停留せず提示値を会合状態 abX に置き換え、後続の b を呑む。
        //     m>=2 の上流 = ストリーム、m=1 は対岸の正常セルの表示(複合 carrier)。
        if self.a3 && self.signal_only && self.abx_state.is_some() {
            for c in 1..=n {
                if !self.mask[c] || c <= 1 || self.mask[c - 1] {
                    continue; // D_i 左端(左隣が正常)だけが提示先になる
                }
                if self.l_buf[c] == 0 || !self.t3_carriers.contains(&self.l_buf[c]) {
                    continue;
                }
                let upstream_is_b = if self.mask[c + 1] {
                    self.br_carriers.contains(&self.to_left[c + 1])
                } else {
                    self.br_carriers.contains(&current[c + 1])
                };
                if upstream_is_b {
                    self.to_left[c] = self.abx_state.unwrap();
                    self.l_buf[c] = 0;
                    self.ab_consume[c] = true;
                }
            }
        }
    }

    // (a) 純粋シフト線モードの 1 セル分。
    //   故障セル: to_right ← 左隣の生の状態(左隣が故障ならその to_right)、
    //             to_left ← 右隣の生の状態(右隣が故障ならその to_left)。判断なし。
    //   正常セル: 左入力 = 左隣が故障なら to_right[c-1](生)、そうでなければ
    //             PJ(左隣の状態)。右入力も同様。境界セル(どちらかの隣が故障)は
    //             bmap[(flags,l,c,r)] → 無ければ rules[PL(l)][c][PR(r)]。
    fn pure_step(&mut self, c: usize, current: &[St], next_cells: &mut [St], config: &Config) {
        if self.mask[c] {
            self.to_right_nex[c] = if self.mask[c - 1] { self.to_right[c - 1] } else { current[c - 1] };
            self.to_left_nex[c] = if self.mask[c + 1] { self.to_left[c + 1] } else { current[c + 1] };
            next_cells[c] = current[c];
            return;
        }
        let fl = self.mask[c - 1];
        let fr = self.mask[c + 1];
        let l = if fl { self.to_right[c - 1] } else { config.pj[current[c - 1] as usize] };
        let r = if fr { self.to_left[c + 1] } else { config.pj[current[c + 1] as usize] };
        let cur = current[c];
        let flags: u8 = (fl as u8) | ((fr as u8) << 1);
        let v = if flags != 0 {
            // 境界表は射影後の入力で引く(生の入力の区別は PL/PR が別の信号状態に
            // 写すことで表す: 例 LNG→NG、LNG2→NG2)。無ければ密な表。
            let l2 = if fl { config.pl[l as usize] } else { l };
            let r2 = if fr { config.pr[r as usize] } else { r };
            if let Some(&t) = config.bmap.get(&(flags, l2, cur, r2)) {
                t
            } else if let Some(&t) = config.bmap.get(&(0, l2, cur, r2)) {
                t
            } else if (l2 as usize) >= (1 << BITS) || (cur as usize) >= (1 << BITS) || (r2 as usize) >= (1 << BITS) {
                // 拡張状態を含む鍵は疎な表にしか無い: 未定義
                if self.trace_fb {
                    eprintln!(
                        "FB(pure B{} ext): cell{} raw ({}, {}, {}) -> proj ({}, {}, {})",
                        flags,
                        c,
                        config.states[l as usize].name.trim(),
                        config.states[cur as usize].name.trim(),
                        config.states[r as usize].name.trim(),
                        config.states[l2 as usize].name.trim(),
                        config.states[cur as usize].name.trim(),
                        config.states[r2 as usize].name.trim()
                    );
                }
                0
            } else {
                let kidx = ((l2 as usize) << (2 * BITS)) + ((cur as usize) << BITS) + (r2 as usize);
                let v = config.rules[kidx];
                if v == 0 && self.trace_fb && (config.defined[kidx / 64] >> (kidx % 64)) & 1 == 0 {
                    eprintln!(
                        "FB(pure B{}): cell{} raw ({}, {}, {}) -> proj ({}, {}, {})",
                        flags,
                        c,
                        config.states[l as usize].name.trim(),
                        config.states[cur as usize].name.trim(),
                        config.states[r as usize].name.trim(),
                        config.states[l2 as usize].name.trim(),
                        config.states[cur as usize].name.trim(),
                        config.states[r2 as usize].name.trim()
                    );
                }
                v
            }
        } else if (l as usize) >= (1 << BITS) || (cur as usize) >= (1 << BITS) || (r as usize) >= (1 << BITS) {
            match config.bmap.get(&(0, l, cur, r)) {
                Some(&t) => t,
                None => {
                    if self.trace_fb {
                        eprintln!(
                            "FB(pure ext): cell{} ({}, {}, {})",
                            c,
                            config.states[l as usize].name.trim(),
                            config.states[cur as usize].name.trim(),
                            config.states[r as usize].name.trim()
                        );
                    }
                    0
                }
            }
        } else {
            let kidx = ((l as usize) << (2 * BITS)) + ((cur as usize) << BITS) + (r as usize);
            let v = config.rules[kidx];
            if v == 0 && self.trace_fb && (config.defined[kidx / 64] >> (kidx % 64)) & 1 == 0 {
                eprintln!(
                    "FB(pure): cell{} ({}, {}, {})",
                    c,
                    config.states[l as usize].name.trim(),
                    config.states[cur as usize].name.trim(),
                    config.states[r as usize].name.trim()
                );
            }
            v
        };
        next_cells[c] = v;
    }

    // 1 ステップ: cur の状態から全セルの t+1 状態を next_cells に計算してから
    // ストリーム/制御チャネルを進める(更新順序に結果が依存しない)。
    fn fault_step(&mut self, current: &mut [St], next_cells: &mut [St], config: &Config) {
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

        if !self.pure {
            self.premerge(current);
        }

        // 2) 全セルの次状態を計算(故障セルはシフト、正常セルはルール表)
        for c in 1..=n {
            if self.pure {
                self.pure_step(c, current, next_cells, config);
                continue;
            }
            if self.mask[c] {
                // 故障セル: 左に由来する値を保ったまま右へ渡す準備をする。
                // 左隣の値は、左隣が故障ならその L→R ストリーム、正常ならそのセルの
                // 現在状態。A1 モードでは故障区間は信号だけを運ぶ: 始端(隣が正常の
                // 場所)では計画された注入信号を取り込み、それ以外は www を充填する
                // (管からセル状態の生値を取り込まない)。
                let w = self.w_state;
                // D_i 左端 pickup(0 成本: 境界セルの表示の時刻の次ステップに故障区間の
                // 先頭セルに入る = 論文の「反射の到着吸収」)。carrier(CB = 凍結
                // 起動 Carrier / TR = 1/1 信号の継続の対岸横断)の表示のときだけ
                // 取り込み、それ以外はアイドル www。
                let trv = if self.mask[c - 1] {
                    self.to_right[c - 1]
                } else if self.signal_only {
                    let d = current[c - 1];
                    // D_i 左端 pickup(②-B per-pipe 化): CB(I_i の凍結起動担体)と NG
                    // (会合放出 {X}NG)はどの故障区間の左端でも取り込む。
                    //   - TR(1/1 信号=I1 担体の縦断)はどこでも取り込む(=縦断の継続)。
                    //   - A3 ②-C: 表示の前進成分(bF/aF、複合 bF⊕bw/aF⊕bw/CB⊕bw の
                    //     前進部分)を射影して取り込む(故障区間は「線に入る信号」だけを運ぶ)。
                    // this_pair_deliverable には右端セルを渡す(m=1 のときは
                    // 左端=右端)。
                    let distal = self
                        .pipes
                        .iter()
                        .find(|&&(s, _)| s == c)
                        .map(|&(_, e)| e);
                    let cb_ok = self.is_pipe_entry(c)
                        && (self.a3
                            || self.pipes.first().map_or(false, |&(s, _)| s == c)
                            || distal
                                .map_or(false, |e| self.this_pair_deliverable(e)));
                    let f = if self.a3 { self.fw_proj[d as usize] } else { None };
                    if (Some(d) == self.cb_state && cb_ok) || Some(d) == self.tr_state {
                        d
                    } else if !self.a3 && Some(d) == self.cbt_state {
                        // 1 セル区間の担体横断表示 cBT = 1/1 信号の継続(TR として運ぶ)
                        self.tr_state.unwrap_or(d)
                    } else if self.ng_carriers.contains(&d) && cb_ok {
                        // 担体 {X}NG / xNG は純 NG に射影して運ぶ
                        self.ng_state.unwrap_or(d)
                    } else if self.a3 && self.is_abf[d as usize] && self.abf_state.is_some() {
                        // 前進対 aF⊕bF の表示: 両成分を取り込む(左端セルが分解する)
                        self.abf_state.unwrap()
                    } else if self.a3 && f.is_some() && (f != self.cb_state || cb_ok) {
                        f.unwrap()
                    } else {
                        // アイドル充填: 右行き線は L を提示する((www,M,L) との
                        // 誤発火回避、new_ctx の idle_to_right と同じ理屈)
                        if self.signal_only { config.soldier as St } else { config.external as St }
                    }
                } else {
                    current[c - 1]
                };
                let idle_r = if self.signal_only { config.soldier as St } else { config.external as St };
                // ②-E: 初回 pickup で左隣が将軍 M(1 セル I1、t=0)なら担体 CB と
                // して拾う(将軍の初期表示 = 1/1 信号の原点。故障区間の初期状態による
                // 局所判定)。
                let trv = if self.signal_only && self.is_pipe_entry(c)
                    && self.pipe_virgin[c] && trv == idle_r
                    && self.m_state.is_some() && Some(current[c - 1]) == self.m_state
                    && self.cb_state.is_some()
                {
                    self.cb_state.unwrap()
                } else {
                    trv
                };
                if self.is_pipe_entry(c) && trv != idle_r {
                    self.pipe_virgin[c] = false;
                }
                // ②-E D_i 左端の会合: 会合セルが左端セル(c_meet = n_i+1)になる対
                // (m_i ∈ {n_i−1, n_i})では、規則層の会合鍵と同じ 2 形を D_i 左端が担う:
                //   偶数 S: 左隣(I_i 右端)が S3A 位相で cBT(R2 装填)が到着する瞬間
                //   奇数 S: 左隣が S33 位相で cBT が提示中の瞬間
                // どちらも次ステップに NG を to_right に生成する(時刻は規則層の
                // 会合放出と同じ 2n_i+m_i)。A3 では左端の遅延線で +1 される。
                let trv = if self.signal_only && self.is_pipe_entry(c) && trv == idle_r
                    && self.ng_state.is_some()
                {
                    let inbound_cbt = if self.mask[c + 1] {
                        self.is_cbt_family(self.to_left[c + 1])
                    } else {
                        self.fold_at_distal(c, current[c + 1])
                    };
                    let left = current[c - 1];
                    let meet = (inbound_cbt && self.j_s3a_frozen.contains(&left))
                        || (self.is_cbt_family(self.to_left[c]) && self.j_s33_frozen.contains(&left));
                    if meet { self.ng_state.unwrap() } else { trv }
                } else {
                    trv
                };
                // own_wave: このセルが故障区間の左端なら、自身の D_i の担体(CB)の
                // 入庫を記録(sticky)。
                if self.is_pipe_entry(c) {
                    self.own_wave_nex[c] = if trv == self.cb_state.unwrap_or(255) {
                        true
                    } else if trv == self.tr_state.unwrap_or(255)
                        || self.is_ng_launch(trv)
                    {
                        false
                    } else {
                        self.own_wave[c]
                    };
                }
                // NG 起動中継の 2 歩提示(②-B、A2 の対の会合処理)。
                if self.ng_hold[c] != 0 {
                    self.ng_hold_nex[c] = 0;
                    self.to_right_nex[c] = self.ng_hold[c];
                    self.r_buf_nex[c] = 0;
                    self.ab_hold_nex[c] = 0;
                } else {
                    self.ng_hold_nex[c] = 0;
                    let a3_entry = self.a3 && self.signal_only && self.is_pipe_entry(c)
                        && self.bf_state.is_some();
                    if a3_entry {
                        // A3 ②-C 左端セル:
                        //   (i) CB 入庫 = 対 (a,b) の誕生。ストリームには a 波(aF、
                        //       先発)を書き、次ステップに b 波(bF、1 歩後ろ)を書く
                        //       (one-shot ab_hold)。b が前進停留で遅れ、a が帰還
                        //       停留で遅れるので、間隔は常に >= 1(重ね合わせ無し)
                        //       で、自対の D_i 左端でだけ「a 到着時に b が 1 歩後ろ」。
                        //   (ii) 表示 bF の入庫(=他区間の b が横断してきた)は 1 歩
                        //        遅延線で停留させる(論文: b は I_{i+1}..I_p の左端で
                        //        1 歩停留。誕生区間 D_i では停留しない=局所判定)。
                        //        後続の列(aF)も遅延線を通るので順序は保たれる。
                        //   (iii) ②-E: 停留は b(と NG)だけ。停留中の b と到着した a は
                        //        同じステップに重ねて提示する(aF⊕bF、論文: b が停留中の
                        //        セルに次段の a が追いつく)。前進対の表示 aFbF の入庫は
                        //        a が通過、b が停留で分かれる。
                        let bf = self.bf_state.unwrap();
                        let af = self.af_state.unwrap_or(bf);
                        // 到着成分: pass = 停留しない成分、stall = 停留する成分
                        let (pass, stall): (Option<St>, Option<St>) = if self.ab_hold[c] != 0 {
                            self.ab_hold_nex[c] = 0;
                            (Some(self.ab_hold[c]), None)
                        } else if Some(trv) == self.cb_state {
                            // 誕生: a 波(aF)が先発、b 波(bF)が 1 歩後ろ。 D_i 内では
                            // 自分の D_i マーカー aFo で運ぶ(提示時に aF へ射影)
                            self.ab_hold_nex[c] = bf;
                            (Some(self.afo_state.unwrap_or(af)), None)
                        } else {
                            self.ab_hold_nex[c] = 0;
                            if trv == idle_r {
                                (None, None)
                            } else if trv == bf || self.is_ng_launch(trv) {
                                (None, Some(trv))
                            } else if Some(trv) == self.abf_state {
                                (Some(af), Some(bf))
                            } else {
                                (Some(trv), None)
                            }
                        };
                        // 会合セルの NG 2 歩表示の 2 便目: 次の対(I_{i+1}, I_{i+2})に
                        // 会合機構が不要(I_{i+1} が最終区間)なら呑む(1/3 信号 を
                        // 生まないため。非 A3 の「2 便目を呑む」と同じ判定を遅延線で)。
                        let stall = match stall {
                            Some(v) if self.is_ng_launch(v)
                                && !self.next_pair_deliverable(c)
                                && (self.is_ng_launch(self.r_buf[c]) || self.is_ng_launch(self.to_right[c])) => None,
                            other => other,
                        };
                        let released: Option<St> = if self.r_buf[c] != 0 { Some(self.r_buf[c]) } else { None };
                        self.r_buf_nex[c] = stall.unwrap_or(0);
                        self.to_right_nex[c] = match (released, pass) {
                            (None, None) => idle_r,
                            (Some(r), None) => r,
                            (None, Some(p)) => p,
                            (Some(r), Some(p)) => {
                                if r == bf && p == af && self.abf_state.is_some() {
                                    self.abf_state.unwrap()
                                } else {
                                    if self.trace_fb {
                                        eprintln!("pipe: cell{} forward overlap {} + {}", c,
                                            config.states[r as usize].name.trim(),
                                            config.states[p as usize].name.trim());
                                    }
                                    r
                                }
                            }
                        };
                    } else {
                        self.to_right_nex[c] = trv;
                        self.r_buf_nex[c] = 0;
                        self.ab_hold_nex[c] = 0;
                    }
                    // 2 歩提示のアームは「提示値」で判定する(A3 の左端の遅延線では
                    // 到着 trv と提示値が 1 歩ずれる。非 A3 では同じ値)。
                    let presented = self.to_right_nex[c];
                    if self.intact(c + 1)
                        && self.is_ng_launch(presented)
                        && self.next_pair_deliverable(c)
                    {
                        self.ng_hold_nex[c] = presented;
                    } else if self.intact(c + 1)
                        && !self.next_pair_deliverable(c)
                        && self.is_ng_launch(self.to_right[c])
                        && self.is_ng_launch(trv)
                    {
                        // 配信可能でない対への会合放出: 2 便目を呑む
                        self.to_right_nex[c] = idle_r;
                    }
                }
                // ── R→L ストリーム ──────────────────────────────────────
                // D_i 右端 pickup(対岸の正常セルの表示から取り込む)。
                //   A3: 帰還成分(T3/bR、複合の帰還部分)を射影し、D_i 提示状態
                //       WT3/WbR(境界マーカー W 付き)として運ぶ。
                //   p=1(非 A3): FT 変換(解凍)と TR (R2 装填の源流)。
                //   p>=2(非 A3): TR のみ、own_wave の故障区間だけ。
                let distal_pick = |d_next: St| -> Option<St> {
                    if self.a3 && self.signal_only {
                        // ②-D: 自分の D_i の a 波(=1/1 信号の継続)が I_{i+1} 左端に表示された
                        // ステップに cBT を折り返す(R2 装填。A2 の cBT 表示 pickup と
                        // 同時刻)。own_wave は折り返し後に落とす(one-shot)。
                        let fold = self.fold_at_distal(c, d_next);
                        let wave = match self.bw_proj[d_next as usize] {
                            Some(b) if Some(b) == self.t3_state => self.wt3_state,
                            Some(b) if Some(b) == self.br_state => self.wbr_state,
                            Some(b) if Some(b) == self.t3br_state => self.t3br_state,
                            _ => None,
                        };
                        match (fold, wave) {
                            (false, w) => w,
                            (true, None) => self.cbt_state,
                            (true, Some(w)) if Some(w) == self.wt3_state => self.cbt_t3_state,
                            (true, Some(w)) if Some(w) == self.wbr_state => self.cbt_br_state,
                            (true, Some(w)) if Some(w) == self.t3br_state => self.cbt_t3br_state,
                            (true, Some(w)) => Some(w),
                        }
                    } else {
                        // 非 A3: 折り返し(自分の D_i の担体の cBT/即時反射 T3 表示)と、
                        // p=1 の解凍変換(T3 族 → FT)。同時なら複合 cBTFT。
                        let fold = self.fold_at_distal(c, d_next);
                        let base = if self.p == 1 && self.d1.map_or(true, |(_, e)| c == e) {
                            self.pickup_thaw(d_next)
                        } else {
                            None
                        };
                        match (fold, base) {
                            (true, Some(b)) if Some(b) == self.ft_state => {
                                self.cbt_ft_state.or(self.cbt_state)
                            }
                            (true, _) => self.cbt_state,
                            (false, b) => b,
                        }
                    }
                };
                self.to_left_nex[c] = if self.signal_only && c > 1 && !self.mask[c - 1] {
                    // D_i 左端(左隣が正常セル)。入来信号を提示に優先させる。
                    let mut inbound: Option<St> = if self.mask[c + 1] {
                        let v = self.to_left[c + 1];
                        if v == config.external as St { None } else { Some(v) }
                    } else {
                        distal_pick(current[c + 1])
                    };
                    if self.a3 && self.signal_only {
                        // ②-D: 入来値を折り返し成分(cBT、停留しない)と帰還波成分
                        // (WT3/WbR 族、a は停留)に分ける。
                        // 成分分解: 折り返し cBT(停留しない)、a 帰還 WT3(停留)、
                        // b 帰還 WbR(停留しない)。帰還対 T3bR は a と b に分かれる。
                        let (fold, wave_a, mut wave_b): (bool, Option<St>, Option<St>) = match inbound {
                            Some(v) if Some(v) == self.cbt_state => (true, None, None),
                            Some(v) if self.cbt_t3_state == Some(v) => (true, self.wt3_state, None),
                            Some(v) if self.cbt_br_state == Some(v) => (true, None, self.wbr_state),
                            Some(v) if self.cbt_t3br_state == Some(v) => (true, self.wt3_state, self.wbr_state),
                            Some(v) if self.is_t3br[v as usize] => (false, self.wt3_state, self.wbr_state),
                            Some(v) if self.t3_carriers.contains(&v) => (false, Some(v), None),
                            Some(v) => (false, None, Some(v)),
                            None => (false, None, None),
                        };
                        // 会合済みの左端: 後続の b 脚(bR 族)を呑む(消費)
                        if self.ab_consume[c] {
                            if wave_b.map_or(false, |v| self.br_carriers.contains(&v)) {
                                wave_b = None;
                                self.ab_consume_nex[c] = true;
                            } else {
                                self.ab_consume_nex[c] = false;
                            }
                        } else {
                            self.ab_consume_nex[c] = false;
                        }
                        // a 帰還の停留 = D_i 左端の 1 歩遅延線(論文: a は帰還時に各
                        // 故障区間の左端で 1 歩停留)。b は止まらない。
                        let out_a: Option<St> = if self.l_buf[c] != 0 { Some(self.l_buf[c]) } else { None };
                        self.l_buf_nex[c] = wave_a.unwrap_or(0);
                        let out_wave: Option<St> = match (out_a, wave_b) {
                            (None, w) => w,
                            (Some(a), None) => Some(a),
                            (Some(a), Some(b)) => {
                                // 停留明けの a と到着 b の同時提示 = 自対の会合は premerge
                                // が abX にしている。ここに来るのは論文条件外の配置。
                                if self.trace_fb {
                                    eprintln!("pipe: cell{} a/b overlap at entry (non-paper layout)", c);
                                }
                                self.l_buf_nex[c] = if self.l_buf_nex[c] == 0 { b } else { self.l_buf_nex[c] };
                                Some(a)
                            }
                        };
                        // 折り返しと波が同じステップなら複合で提示
                        inbound = match (fold, out_wave) {
                            (false, w) => w,
                            (true, None) => self.cbt_state,
                            (true, Some(w)) if Some(w) == self.wt3_state => self.cbt_t3_state,
                            (true, Some(w)) if Some(w) == self.wbr_state => self.cbt_br_state,
                            (true, Some(w)) => Some(w),
                        };
                    }
                    match inbound {
                        Some(v) => v,
                        None => {
                            // D_i 左端(左行き線)のアイドル。解凍信号(FT / 会合 abX)の提示ステップの
                            // 直後にアイドルを FT2(解凍後位相)へ切り替え、以後保つ。
                            let cur = self.to_left[c];
                            if (self.ft2_state.is_some()
                                && (Some(cur) == self.ft_state
                                    || Some(cur) == self.abx_state
                                    || Some(cur) == self.cbt_ft_state))
                                || self.ft2_state == Some(cur)
                            {
                                self.ft2_state.unwrap()
                            } else if w.is_some() {
                                w.unwrap()
                            } else {
                                config.external as St
                            }
                        }
                    }
                } else if self.mask[c + 1] {
                    self.to_left[c + 1]
                } else if self.signal_only {
                    distal_pick(current[c + 1]).unwrap_or(config.external as St)
                } else {
                    current[c + 1]
                };
                // ②-H: D_i 右端の 1 歩記憶(このステップに提示した値が自分の担体か)
                if self.signal_only && !self.mask[c + 1] {
                    let v = self.to_right[c];
                    self.own_pres_nex[c] = Some(v) == self.cb_state || Some(v) == self.afo_state;
                }
                // ②-D: D_i 右端で cBT を折り返したら own_wave を落とす(one-shot)
                if self.signal_only && !self.mask[c + 1]
                    && self.cbt_state.is_some()
                    && (Some(self.to_left_nex[c]) == self.cbt_state
                        || Some(self.to_left_nex[c]) == self.cbt_t3_state
                        || Some(self.to_left_nex[c]) == self.cbt_br_state
                        || Some(self.to_left_nex[c]) == self.cbt_t3br_state)
                {
                    let entries: Vec<usize> = self.pipes.iter()
                        .filter(|&&(_, e)| e == c).map(|&(s, _)| s).collect();
                    for s in entries {
                        self.own_wave_nex[s] = false;
                    }
                }
                // 故障セルの基本状態は変わらない(遷移規則は使わない)
                next_cells[c] = current[c];
            } else {
                // ルール入力: 隣が故障セルなら、こちらへ向かって流れてきた
                // ストリーム値を見る(左隣からは L→R ストリーム、右隣からは
                // R→L ストリーム)。
                let left = if self.mask[c - 1] {
                    let v = self.to_right[c - 1];
                    if self.afo_state == Some(v) { self.af_state.unwrap_or(v) } else { v }
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
                    let v = config.rules[((left as usize) << (2 * BITS)) + ((current[c] as usize) << BITS) + (right as usize)];
                    // 診断: 未定義鍵(状態 0=FB への遷移)を環境変数 FSSP_TRACE_FB で表示
                    let kidx = ((left as usize) << (2 * BITS)) + ((current[c] as usize) << BITS) + (right as usize);
                    if v == 0 && self.trace_fb && (config.defined[kidx / 64] >> (kidx % 64)) & 1 == 0 {
                        eprintln!(
                            "FB: cell{} ({}, {}, {})",
                            c,
                            config.states[left as usize].name.trim(),
                            config.states[current[c] as usize].name.trim(),
                            config.states[right as usize].name.trim()
                        );
                    }
                    v
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
        std::mem::swap(&mut self.ng_hold, &mut self.ng_hold_nex);
        std::mem::swap(&mut self.ab_hold, &mut self.ab_hold_nex);
        std::mem::swap(&mut self.r_buf, &mut self.r_buf_nex);
        std::mem::swap(&mut self.l_buf, &mut self.l_buf_nex);
        std::mem::swap(&mut self.ab_consume, &mut self.ab_consume_nex);
        std::mem::swap(&mut self.own_wave, &mut self.own_wave_nex);
        std::mem::swap(&mut self.own_pres, &mut self.own_pres_nex);
    }

    // 凍結状態のダンプ(-f/--inject 指定時のみ)。凍結セルに * を出す。
    // A1 モードでは、凍結変状態 F(x) を表示しているセル(ルール層で凍結)も * で
    // 示す(FA/FB/FC/FL/FM/FQ/FR = 2 文字の F で始まる状態名)。
    // 各行の列幅は状態行と揃えて位置を対応させる。
    // 正常セルが凍結変状態 F(x)(FA/FB/FC/FL/FM/FQ/FR)を表示しているか
    fn rule_frozen(&self, c: usize, current: &[St], config: &Config) -> bool {
        let name = config.states[current[c] as usize].name.trim();
        self.intact(c)
            && name.len() == 2
            && name.starts_with('F')
            && matches!(name.chars().nth(1), Some('A' | 'B' | 'C' | 'L' | 'M' | 'Q' | 'R'))
    }

    fn dump_frozen(&self, current: &[St], config: &Config) {
        let mut row = String::from("F:");
        for c in 1..current.len() - 1 {
            let width = config.states[current[c] as usize].name.len();
            let rule_frozen = self.frozen[c] || self.rule_frozen(c, current, config);
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
    fn dump_streams(&self, current: &[St], config: &Config, prefix: &str) {
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
fn fired_intact(cells: &[St], mask: &[bool], firing: usize) -> bool {
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
// signals は A1/A2 スケジュール信号(plan_a1_signals の出力)。時刻 t に故障区間の
// 始端セルへ値を置き、1/1 で流れて終端で提示される。
// メッセージは「時刻 t にその時刻のセルに置かれる=そのステップで到着処理される」。
// 戻り値は終了した時刻(正常セル全発火または上限)。
fn simulate_fault(
    current: &mut Vec<St>,
    next_cells: &mut Vec<St>,
    ff: &mut FaultField,
    config: &Config,
    cap: usize,
    injections: &[(Ctrl, usize, usize)],
    wallsets: &[(St, usize)],
    signals: &[PlannedSignal],
    dump: bool,
    color: bool,
    mut trace: Option<&mut Trace>,
) -> usize {
    let n = current.len() - 2;
    let mut t = 0;
    loop {
        // この時刻の信号注入(故障区間の始端セルに値を置く→ 1/1 で流れて終端に提示)
        for sig in signals {
            if sig.time == t {
                if sig.to_left {
                    // A2 の FT 注入が故障区間の折り返し cBT と同じステップに重なる
                    // (I_{i+1} が 1 セル: 担体の即時壁反射と解凍時刻の一致)
                    // ときは複合 cBTFT で提示する(D_i の内容を上書きしない)。
                    if Some(sig.value) == ff.ft_state && ff.is_cbt_family(ff.to_left[sig.cell]) {
                        ff.to_left[sig.cell] = ff.cbt_ft_state.unwrap_or(sig.value);
                    } else {
                        ff.to_left[sig.cell] = sig.value;
                    }
                } else {
                    ff.to_right[sig.cell] = sig.value;
                    // 注入始発の NG(会合放出の注入代替): D_i 右端で 2 歩提示
                    // する。始端 pickup を通らない直接書き込みは誕生キー
                    // (NG,M,r) の保持提示をアームしないので、ここで ng_hold
                    // (現行値)に直接アームする(m=1 のとき始端=右端で
                    // これが唯一の経路。m>=2 では中継の到着が自然にアーム)。
                    // 次ステップの fault 枝がこれを提示して消す。
                    if ff.is_ng_launch(sig.value)
                        && ff.intact(sig.cell + 1)
                        && ff.next_pair_deliverable(sig.cell)
                    {
                        ff.ng_hold[sig.cell] = sig.value;
                    }
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
        ff.premerge(&current[..]);
        if let Some(tr) = trace.as_deref_mut() {
            for sig in signals {
                if sig.time == t {
                    let name = config.states[sig.value as usize].name.trim();
                    let dir = if sig.to_left { "R→L" } else { "L→R" };
                    tr.notes.push((t, format!("{}@cell{} ({})", name, sig.cell, dir)));
                }
            }
            tr.push(current, Some(ff), config);
        }
        if dump {
            // 計画信号の提示時刻の注記(どの信号がどの故障区間に乗ったかを
            // ダンプ上で追えるようにする。freeze_tools のパーサは
            // sig: プレフィックス行をスキップする)
            for sig in signals {
                if sig.time == t {
                    let name = config.states[sig.value as usize].name.trim();
                    let dir = if sig.to_left { "R→L" } else { "L→R" };
                    let role = if name == "FT" {
                        "解凍"
                    } else if name == "FS" {
                        "凍結"
                    } else if name.len() >= 3 && name.ends_with("NG") {
                        "将軍起動"
                    } else {
                        "信号"
                    };
                    println!("sig: t={} {}@cell{} ({})", t, name, sig.cell, dir);
                }
            }
            // 時刻 t の状態(その時刻に提示される信号を含む D_i の内容)を出す
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

// ── 一括モード(--batch <file>) ─────────────────────────────────────────
// 規則表を 1 回だけ読み、ファイルの各行 `n spec [a3]` を順に -f 実行して
// 1 行ずつ結果を出す(スイープ用。1 配置 1 プロセスだと規則表の読込と 256 MB
// の表の確保が毎回かかる)。出力形式:
//   batch: n=<n> f=<spec> a3=<0|1> <SYNCED|NOT SYNCED> t=<t> injected=<k>
// 配置の条件(n_i >= m_i 等)の検査は呼び出し側(freeze_tools.py)が行う。
fn batch_run_layout(config: &Config, n: usize, spec: &str, a3: bool, pure: bool) -> Result<(bool, usize, usize), String> {
    let fault = parse_fault_spec(spec, n)?;
    let analysis = analyze_faults(&fault, n);
    let mask = analysis.mask.clone();
    let d1 = Some((analysis.segments[1].start, analysis.segments[1].end));
    let pipes: Vec<(usize, usize)> = analysis
        .segments
        .iter()
        .filter(|s| !s.intact)
        .map(|s| (s.start, s.end))
        .collect();
    let p_cnt = pipes.len();
    let mut ff = FaultField::new_ctx(&mask, config, true, p_cnt, d1, pipes, a3, pure);
    let (signals, _notes) = plan_a1_signals(&analysis, config, a3);
    let cap = if a3 { (n << 1) - 2 + p_cnt } else { (n << 1) - 2 };
    let mut current = first_line(n, config);
    let mut next_cells = new_line(n, config);
    let t = simulate_fault(
        &mut current,
        &mut next_cells,
        &mut ff,
        config,
        cap,
        &[],
        &[],
        &signals,
        false,
        false,
        None,
    );
    Ok((fired_intact(&current, &mask, config.firing), t, signals.len()))
}

fn run_batch(config: &Config, path: &str, pure: bool) {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("--batch: cannot read {}: {}", path, e);
            std::process::exit(1);
        }
    };
    let start = Instant::now();
    let mut count = 0usize;
    for (lineno, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() < 2 {
            eprintln!("--batch: line {}: expect `n spec [a3]`: {}", lineno + 1, line);
            continue;
        }
        let n = match parts[0].parse::<usize>() {
            Ok(v) if v >= 2 => v,
            _ => {
                eprintln!("--batch: line {}: invalid n: {}", lineno + 1, parts[0]);
                continue;
            }
        };
        let spec = parts[1];
        let a3 = parts.get(2).map_or(false, |v| *v == "a3" || *v == "--a3" || *v == "1");
        match batch_run_layout(config, n, spec, a3, pure) {
            Ok((synced, t, injected)) => {
                println!(
                    "batch: n={} f={} a3={} {} t={} injected={}",
                    n,
                    spec,
                    if a3 { 1 } else { 0 },
                    if synced { "SYNCED" } else { "NOT SYNCED" },
                    t,
                    injected
                );
            }
            Err(e) => println!("batch: n={} f={} a3={} ERROR {}", n, spec, if a3 { 1 } else { 0 }, e),
        }
        count += 1;
    }
    let end = start.elapsed();
    println!("batch: {} layouts, time: {}.{:03}s", count, end.as_secs(), end.subsec_nanos() / 1_000_000);
}

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
    let mut pure_mode = false;
    let mut size_arg: Option<usize> = None;
    let mut rule_arg: Option<String> = None;
    let mut fault_arg: Option<String> = None;
    let mut steps_arg: Option<usize> = None;
    let mut batch_arg: Option<String> = None;
    let mut html_arg: Option<String> = None;
    let mut json_arg: Option<String> = None;
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
        "  --batch <file>        一括モード: 各行 `n spec [a3]` を規則表 1 回読込で順に
",
        "                        -f 実行し `batch: n=.. f=.. a3=.. SYNCED t=.. injected=..` を出す
",
        "  --pure                故障セルを純粋シフト線にする((a) の模型、A3 専用)
",
        "  --strict              n_i>=m_i (A1/A2 条件) を満たす配置以外は実行しない\n",
        "  -d, --dump            1 ステップごとの途中経過を出力(ruby -d と同書式)\n",
        "                        (-f/--inject 時は R:/L:/F: 凍結行も追加)\n",
        "  -c, --color           ダンプを ANSI 色付きに(-c は -d を兼ねる)\n",
        "  --html <file>         時空図を見るブラウザ UI(自己完結 HTML)を書き出す\n",
        "                        (-d と同じ内容: 状態行、R:/L: 線、凍結、sig: 注記)\n",
        "  --json <file>         同じ内容を JSON で書き出す(viewer.html で開ける)\n",
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
            "--pure" => pure_mode = true,
            "-r" | "--rule" | "-s" | "--size" | "-f" | "--fault" | "--inject" | "--wallset" | "-t" | "--steps" | "--batch" | "--html" | "--json" => {
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
                } else if opt == "--batch" {
                    batch_arg = Some(value.clone());
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
                } else if opt == "--html" {
                    html_arg = Some(value.clone());
                } else if opt == "--json" {
                    json_arg = Some(value.clone());
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

    if let Some(path) = batch_arg.as_ref() {
        run_batch(&config, path, pure_mode);
        std::process::exit(0);
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
        // A1 モード: -f 指定で --inject/--wallset なし。故障区間は信号だけを運び、
        // Umeo A1/A2 の式から区間構造より算出した時刻で凍結/解凍/将軍起動を提示する。
        let signal_only = fault_arg.is_some() && injections.is_empty() && wallsets.is_empty();
        // 左行き線(to_left)の変換 pickup は p=1 のときだけ有効(他区間の反射波が
        // 隔離: 左行き線(to_left)の FT 変換 pickup は p=1 の D1 だけ有効(p>=2 では
        // 各区間の解凍を注入 FT が担う。他区間の反射波が解凍を起動するのを防ぐ)。
        // D_i 左端(to_right)の CB pickup も D1 だけ(1/1 信号は 1 本なので)。
        let d1 = analysis
            .as_ref()
            .map(|a| (a.segments[1].start, a.segments[1].end));
        let pipes: Vec<(usize, usize)> = analysis
            .as_ref()
            .map(|a| {
                a.segments
                    .iter()
                    .filter(|s| !s.intact)
                    .map(|s| (s.start, s.end))
                    .collect()
            })
            .unwrap_or_default();
        let p_cnt = pipes.len();
        let mut ff = FaultField::new_ctx(
            &mask,
            &config,
            signal_only,
            p_cnt,
            d1,
            pipes.clone(),
            a3_mode,
            pure_mode,
        );
        if pure_mode && !a3_mode {
            eprintln!("warning: --pure is designed for --a3 (A1/A2 injections are not supported in pure mode)");
        }
        // 純粋シフト線の規則表(PL/PR/PJ/B1/B2 行)を持たないファイルでは境界セルの
        // 鍵が引けず必ず NOT SYNCED になる(full/基底の規則表との取り違えが多い)。
        if pure_mode && config.bmap.is_empty() {
            eprintln!(
                "error: --pure needs the pure-mode rule table (PL/PR/PJ/B1/B2 lines): {} has none.
       use balzer-freeze-pure.rul.txt (generate with `python rust/pure_tools.py add_pure`)",
                rule_file
            );
            std::process::exit(1);
        }

        // A1/A2 の信号スケジュールを区間構造から組み立てる
        let mut a1_notes: Vec<String> = vec![];
        let signals: Vec<PlannedSignal> = match &analysis {
            Some(a) if signal_only => {
                let (plan, notes) = plan_a1_signals(a, &config, a3_mode);
                for note in &notes {
                    println!("a1: {}", note);
                }
                a1_notes = notes;
                plan
            }
            _ => vec![],
        };

        // --wallset の状態名をルールファイルの状態番号に解決する
        let wallsets: Vec<(St, usize)> = wallsets
            .into_iter()
            .map(|(name, t)| {
                match config.states.iter().position(|s| s.name.trim() == name) {
                    Some(idx) => (idx as St, t),
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
        let want_trace = html_arg.is_some() || json_arg.is_some();
        let mut trace = Trace::new();
        trace.header = a1_notes.iter().map(|x| format!("a1: {}", x)).collect();
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
            if want_trace { Some(&mut trace) } else { None },
        );

        let synced = fired_intact(&current, &mask, config.firing);
        if synced {
            println!("SYNCED (intact cells) at t={}", t);
        } else {
            println!("NOT SYNCED (stopped at t={})", t);
        }
        if want_trace {
            let meta: Vec<(&str, String)> = vec![
                ("n", cell_size.to_string()),
                ("faults", json_str(fault_arg.as_deref().unwrap_or(""))),
                ("a3", a3_mode.to_string()),
                ("pure", pure_mode.to_string()),
                ("synced", synced.to_string()),
                ("t", t.to_string()),
                ("rule", json_str(&rule_file)),
                ("cmd", json_str(&args[1..].join(" "))),
            ];
            write_trace_outputs(&html_arg, &json_arg, &trace, &config, &mask, &meta, pure_mode);
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

    let want_trace = html_arg.is_some() || json_arg.is_some();
    if dump_mode || want_trace {
        // ダンプ本体が初期状態と最終状態を出すので、debug 表示は重複になる
        // この分岐では出さない
        let mut trace = Trace::new();
        let t = simulate_dump(
            &mut current,
            &mut next_cells,
            &config,
            cell_size,
            dump_mode,
            color_mode,
            if want_trace { Some(&mut trace) } else { None },
        );
        end = start.elapsed();
        if want_trace {
            let synced = fired(&current, config.firing);
            if !synced {
                println!("NOT SYNCED");
            }
            let mask = vec![false; cell_size + 2];
            let meta: Vec<(&str, String)> = vec![
                ("n", cell_size.to_string()),
                ("faults", json_str("")),
                ("a3", "false".to_string()),
                ("pure", "false".to_string()),
                ("synced", synced.to_string()),
                ("t", t.to_string()),
                ("rule", json_str(&rule_file)),
                ("cmd", json_str(&args[1..].join(" "))),
            ];
            write_trace_outputs(&html_arg, &json_arg, &trace, &config, &mask, &meta, false);
        }
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
        let mut rules: Vec<St> = vec![0 as St; TBL + 2];
        for l in 0 as St..4 {
            for r in 0 as St..4 {
                // 中心 Q: 隣に S がいれば S になる(両方向 1/1 進行)、いなければ Q
                rules[((l as usize) << (2 * BITS)) + (1 << BITS) + (r as usize)] =
                    if l == 2 || r == 2 { 2 } else { 1 };
                // 中心 S: 発信して Q に戻る
                rules[((l as usize) << (2 * BITS)) + (2 << BITS) + (r as usize)] = 1;
            }
        }
        Config {
            states,
            general: 3,
            soldier: 1,
            firing: 3,
            external: 0,
            rules,
            defined: vec![0u64; TBL / 64],
            pl: vec![],
            pr: vec![],
            pj: vec![],
            bmap: std::collections::HashMap::new(),
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
        cur: &mut Vec<St>,
        nex: &mut Vec<St>,
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
        let got = nextcell(config.external as St, config.general as St, config.soldier as St, &config);
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
