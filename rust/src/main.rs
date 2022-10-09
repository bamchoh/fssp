use rayon;
use rayon::prelude::IntoParallelIterator;
use rayon::prelude::ParallelIterator;
use std::env;
use std::fs;
use std::io::{BufReader, Read};
use std::slice::from_raw_parts_mut;
use std::sync::mpsc::{self, channel, sync_channel};
use std::sync::mpsc::{Receiver, Sender, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;
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
    rules: [usize; 4096],
}

fn dump_full(cells: &[usize], config: &Config) {
    print!("|");
    for i in 0..cells.len() {
        print!("{0: <4}|", config.states[cells[i]].name);
    }
    println!();
}

fn color_code(color: &String) -> String {
    let b = i32::from_str_radix(&color[0..2].to_string(), 16).unwrap();
    let g = i32::from_str_radix(&color[2..4].to_string(), 16).unwrap();
    let r = i32::from_str_radix(&color[4..6].to_string(), 16).unwrap();

    format!("{};{};{}", r, g, b)
}

fn dumpln(cells: &[usize], config: &Config) {
    print!("|");
    for i in 1..cells.len() - 1 {
        print!(
            "\x1b[38;2;{1}m\x1b[48;2;{2}m{0: <4}\x1b[0m|",
            config.states[cells[i]].name,
            color_code(&config.states[cells[i]].foreground),
            color_code(&config.states[cells[i]].background),
        );
    }
    println!();
}

fn dumpleft(cells: &[usize], config: &Config) {
    print!("|");
    for i in 1..cells.len() {
        print!(
            "\x1b[38;2;{1}m\x1b[48;2;{2}m{0: <4}\x1b[0m|",
            config.states[cells[i]].name,
            color_code(&config.states[cells[i]].foreground),
            color_code(&config.states[cells[i]].background),
        );
    }
}

fn dumpright(cells: &[usize], config: &Config) {
    for i in 0..cells.len() - 1 {
        print!(
            "\x1b[38;2;{1}m\x1b[48;2;{2}m{0: <4}\x1b[0m|",
            config.states[cells[i]].name,
            color_code(&config.states[cells[i]].foreground),
            color_code(&config.states[cells[i]].background),
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

fn calc_next<'a>(next_cells: &mut [usize], idx: usize, current: &[usize], config: &'a Config) {
    let mut i = idx;
    for cells in current.windows(3) {
        next_cells[i] = nextcell(cells[0], cells[1], cells[2], config);
        i += 1;
    }
}

fn nextcell(left: usize, center: usize, right: usize, config: &Config) -> usize {
    config.rules[(left << 8) + (center << 4) + right]
}

fn nextline<'a>(cur: &mut [usize], nex: &mut [usize], config: &'a Config) {
    calc_next(nex, 1, cur, config);
}

fn nextline_par<'a>(ary_pair: &mut AryPair, config: &'a Config) {
    calc_next(ary_pair.nex, 1, ary_pair.cur, config);
}

fn per_nextline<'a>(current: &[usize], next_cells: &mut [usize], config: &'a Config) {
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
        rules: [0; 4096],
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

                config.rules[i] = j;

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

fn fired(cells: &[usize], firing: usize) -> bool {
    for i in 1..cells.len() - 1 {
        if cells[i] != firing {
            return false;
        }
    }
    true
}

fn new_line<'a>(n: usize, config: &'a Config) -> Vec<usize> {
    let mut cells: Vec<usize> = vec![config.soldier; n + 2];
    cells[0] = config.external;
    let i = cells.len() - 1;
    cells[i] = config.external;
    return cells;
}

fn first_line<'a>(n: usize, config: &'a Config) -> Vec<usize> {
    let mut cells = new_line(n, config);
    cells[1] = config.general;
    cells
}

fn simulate<'a>(
    mut current: &'a mut [usize],
    mut next_cells: &'a mut [usize],
    config: &'a Config,
    n: usize,
) -> usize {
    let mut t = 0;
    while !(fired(current, config.firing) || (t > ((n << 1) - 2))) {
        nextline(current, next_cells, &config);
        t += 1;
        (current, next_cells) = (next_cells, current);

        #[cfg(debug_assertions)]
        dumpln(current, &config);
    }
    t
}

struct AryPair<'a> {
    cur: &'a mut [usize],
    nex: &'a mut [usize],
    sender: [Option<Sender<i32>>; 2],
    receiver: [Option<Receiver<i32>>; 2],
}

fn split<'a>(ary1: &'a mut [usize], ary2: &'a mut [usize], n: isize) -> Vec<AryPair> {
    let ary_len = ary1.len() as isize;
    let n = (ary_len as f64 / n as f64).ceil() as isize;
    let mut idx_ofs = 0;
    let mut splitted_ary: Vec<AryPair> = vec![];

    while idx_ofs < ary_len {
        let (start_idx, size_ofs) = if idx_ofs == 0 {
            (idx_ofs, 1)
        } else {
            (idx_ofs - 1, 2)
        };

        let ptr1 = unsafe { ary1.as_mut_ptr().offset(start_idx) };
        let ptr2 = unsafe { ary2.as_mut_ptr().offset(start_idx) };

        let size = if n + size_ofs + start_idx < ary_len {
            n + size_ofs
        } else {
            ary_len - start_idx
        };

        let sp_ary1 = unsafe { from_raw_parts_mut(ptr1, size as usize) };
        let sp_ary2 = unsafe { from_raw_parts_mut(ptr2, size as usize) };

        splitted_ary.push(AryPair {
            cur: sp_ary1,
            nex: sp_ary2,
            sender: [None, None],
            receiver: [None, None],
        });

        idx_ofs += n;
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
    cur: &'a mut [usize],
    nex: &'a mut [usize],
    config: &'a Config,
    n: usize,
) -> usize {
    let ary;
    ary = split(cur, nex, rayon::current_num_threads() as isize);

    ary.into_par_iter().for_each(|mut ary_pair| {
        let mut t = 0;
        while !(fired(ary_pair.cur, config.firing) || (t > ((n << 1) - 2))) {
            nextline_par(&mut ary_pair, &config);

            for sender in &ary_pair.sender {
                if let Some(ref n) = sender {
                    n.send(t as i32).unwrap();
                };
            }

            for receiver in &ary_pair.receiver {
                if let Some(ref n) = receiver {
                    n.recv().unwrap();
                };
            }

            (ary_pair.cur, ary_pair.nex) = (ary_pair.nex, ary_pair.cur);

            t += 1;

            // #[cfg(debug_assertions)]
            // dumpleft(&left_cur, &config);
            // println!("{}, {:?}", t, ary_pair.cur);
        }
    });
    0
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

    let current = &mut first_line(cell_size, &config)[..];
    let next_cells = &mut new_line(cell_size, &config)[..];

    #[cfg(debug_assertions)]
    dumpln(current, &config);

    let start = Instant::now();

    let t = par_simulate(current, next_cells, &config, cell_size);

    let end = start.elapsed();

    #[cfg(debug_assertions)]
    dumpln(current, &config);

    println!(
        "time: {}({}), fired: {}.{:03}s",
        t,
        2 * cell_size - 2,
        end.as_secs(),
        end.subsec_nanos() / 1_000_000
    );
}
