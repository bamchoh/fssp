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
    rules: [u8; 8192],
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
    config.rules[((left as usize) << 8) + ((center as usize) << 4) + (right as usize)]
}

fn nextline(cur: &mut [u8], nex: &mut [u8], config: &Config) {
    calc_next(nex, 1, cur, config);
}

// AVX2 gather パス。u8 表(8192)をバイトオフセットで参照し、
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

            // idx16 = (l<<8) + (c<<4) + r
            let ilo = _mm256_add_epi16(
                _mm256_add_epi16(_mm256_slli_epi16(llo, 8), _mm256_slli_epi16(clo, 4)),
                rlo,
            );
            let ihi = _mm256_add_epi16(
                _mm256_add_epi16(_mm256_slli_epi16(lhi, 8), _mm256_slli_epi16(chi, 4)),
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
            nex[i] = rules[((cur[i - 1] as usize) << 8)
                + ((cur[i] as usize) << 4)
                + (cur[i + 1] as usize)];
            i += 1;
        }
    }
}

fn nextline_par(ary_pair: &mut AryPair, config: &Config, use_avx2: bool) {
    #[cfg(target_arch = "x86_64")]
    {
        if use_avx2 {
            unsafe { simd::step(ary_pair.cur, ary_pair.nex, &config.rules) };
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
                idx = (idx << 4) + i;
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
        rules: [0; 8192],
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

    ary.into_par_iter().for_each(|mut ary_pair| {
        let mut t = 0;
        while !(fired(ary_pair.cur, config.firing) || (t > ((n << 1) - 2))) {
            nextline_par(&mut ary_pair, config, use_avx2);

            for sender in &ary_pair.sender {
                if let Some(ref s) = sender {
                    s.send(t as i32).unwrap();
                };
            }

            for receiver in &ary_pair.receiver {
                if let Some(ref r) = receiver {
                    r.recv().unwrap();
                };
            }

            (ary_pair.cur, ary_pair.nex) = (ary_pair.nex, ary_pair.cur);

            t += 1;
        }
    });
}

fn main() {
    let args: Vec<String> = env::args().collect();

    let cell_size: usize;

    if args.len() < 2 {
        cell_size = 10;
    } else {
        cell_size = args[1].parse().unwrap();
    }

    let s = match read_file("../waksman-slim.rul.txt".to_owned()) {
        Ok(s) => s,
        Err(e) => panic!("fail to read file: {}", e),
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

    #[cfg(target_arch = "x86_64")]
    let use_avx2 = is_x86_feature_detected!("avx2");
    #[cfg(not(target_arch = "x86_64"))]
    let use_avx2 = false;

    let current = &mut first_line(cell_size, &config)[..];
    let next_cells = &mut new_line(cell_size, &config)[..];

    #[cfg(debug_assertions)]
    dumpln(current, &config);

    let start = Instant::now();

    par_simulate(current, next_cells, &config, cell_size, use_avx2);

    let end = start.elapsed();

    #[cfg(debug_assertions)]
    dumpln(current, &config);

    if !fired(current, config.firing) {
        println!("NOT SYNCED");
    }

    println!(
        "time: {}.{:03}s",
        end.as_secs(),
        end.subsec_nanos() / 1_000_000
    );
}