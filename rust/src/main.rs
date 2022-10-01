use rayon::prelude::*;
use std::env;
use std::fs;
use std::io::{BufReader, Read};
use std::time::Instant;

#[derive(Debug, Clone)]
struct State {
    name: String,
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

fn dump(current: &Vec<usize>, config: &Config) {
    print!("|");
    for i in 1..current.len() - 1 {
        print!("{0: <4}|", config.states[current[i]].name);
    }
    println!();
}

#[derive(Debug)]
enum ParseState {
    Begin,
    FoundState(i32),
    FoundRule(i32),
}

fn set_next<'a>(next_cell: &mut usize, cells: &[usize], config: &'a Config) {
    *next_cell = get_next(cells, config);
}

fn get_next<'a>(cells: &[usize], config: &'a Config) -> usize {
    config.rules[(cells[0] << 8) + (cells[1] << 4) + cells[2]]
}

fn calc_next<'a>(next_cells: &mut [usize], idx: usize, current: &[usize], config: &'a Config) {
    let mut i = idx;
    for cells in current.windows(3) {
        set_next(&mut next_cells[i], cells, config);
        i += 1;
    }
}

fn nextline<'a>(current: &Vec<usize>, next_cells: &mut Vec<usize>, config: &'a Config) {
    calc_next(next_cells, 1, &current[..], config);
}

fn per_nextline<'a>(current: &Vec<usize>, config: &'a Config) -> Vec<usize> {
    let mut next_cells = new_line(current.len() - 2, &config);
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
    next_cells
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

fn fired(cells: &Vec<usize>, config: &Config) -> bool {
    for i in 1..cells.len() - 1 {
        if cells[i] != config.firing {
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

    let mut cells = first_line(cell_size, &config);

    let start = Instant::now();

    #[cfg(debug_assertions)]
    dump(&cells, &config);

    let mut t = 0;
    while !(fired(&cells, &config) || (t > (2 * cell_size - 2))) {
        let mut next_cells = new_line(cells.len() - 2, &config);
        nextline(&cells, &mut next_cells, &config);
        cells = next_cells;
        t += 1;

        #[cfg(debug_assertions)]
        dump(&cells, &config);
    }

    let end = start.elapsed();

    println!(
        "time: {}({}), fired: {}.{:03}s",
        t,
        2 * cell_size - 2,
        end.as_secs(),
        end.subsec_nanos() / 1_000_000
    );
}
