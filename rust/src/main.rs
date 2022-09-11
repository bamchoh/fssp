use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::{BufReader, Read};
use std::time::Instant;

#[derive(Debug)]
struct Config<'a> {
    states: HashMap<&'a str, State<'a>>,
    general: &'a State<'a>,
    soldier: &'a State<'a>,
    firing: &'a State<'a>,
    external: &'a State<'a>,
    rules: HashMap<&'a str, HashMap<&'a str, HashMap<&'a str, &'a str>>>,
}

struct Env<'a> {
    config: &'a Config<'a>,
}

impl<'a> Env<'a> {
    fn dump(&self, current: &Vec<&'a State<'a>>) {
        print!("|");
        for i in 1..current.len() - 1 {
            print!("{0: <4}|", current[i].name);
        }
        println!();
    }

    fn next_lines(&mut self, current: &Vec<&'a State<'a>>, next_cells: &mut Vec<&'a State<'a>>) {
        for i in 1..current.len() - 1 {
            let left = current[i - 1].name;
            let center = current[i].name;
            let right = current[i + 1].name;

            let l_map = &self.config.rules;

            match l_map.get(left) {
                Some(c_map) => match c_map.get(center) {
                    Some(r_map) => match r_map.get(right) {
                        Some(next) => {
                            let next_state = &self.config.states[next];
                            next_cells[i] = next_state;
                        }
                        None => {}
                    },
                    None => {}
                },
                None => {}
            }
        }
    }
}

const EMPTY_STATE: &State = &State {
    name: "",
    class: "",
};

#[derive(Debug)]
struct State<'a> {
    name: &'a str,
    class: &'a str,
}

#[derive(Debug)]
struct Rule<'a> {
    left: &'a str,
    center: &'a str,
    right: &'a str,
    next: &'a str,
}

#[derive(Debug)]
enum ParseState {
    Begin,
    FoundState(i32),
    FoundRule(i32),
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
    let v: Vec<&str> = line.split(&[' ', ','][..]).filter(|&x| x != "").collect();

    let state = State {
        name: v[0],
        class: v[3],
    };

    if n == 1 {
        (ParseState::Begin, state)
    } else {
        (ParseState::FoundState(n - 1), state)
    }
}

fn parse_rules(line: &str, n: i32) -> (ParseState, Rule) {
    let v: Vec<&str> = line
        .split(&[' ', '#', '-', '>'][..])
        .filter(|&x| x != "")
        .collect();

    let rule = Rule {
        left: v[0],
        center: v[1],
        right: v[2],
        next: v[3],
    };

    if n == 1 {
        (ParseState::Begin, rule)
    } else {
        (ParseState::FoundRule(n - 1), rule)
    }
}

fn parse_rule_file(s: &String) -> Config {
    let mut parse_state = ParseState::Begin;
    let mut config = Config {
        states: HashMap::new(),
        rules: HashMap::new(),
        general: EMPTY_STATE,
        soldier: EMPTY_STATE,
        firing: EMPTY_STATE,
        external: EMPTY_STATE,
    };

    for line in s.lines() {
        parse_state = match parse_state {
            ParseState::Begin => parse_begin(&line),
            ParseState::FoundState(n) => {
                let (next_state, state) = parse_states(&line, n);
                config.states.insert(state.name, state);
                next_state
            }
            ParseState::FoundRule(n) => {
                let (next_state, rule) = parse_rules(&line, n);

                if !config.rules.contains_key(rule.left) {
                    config.rules.insert(rule.left, HashMap::new());
                }

                let center = config.rules.get_mut(rule.left).unwrap();
                if !center.contains_key(rule.center) {
                    center.insert(rule.center, HashMap::new());
                }

                let right = center.get_mut(rule.center).unwrap();
                if !right.contains_key(rule.right) {
                    right.insert(rule.right, rule.next);
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

fn fired(cells: &Vec<&State>, config: &Config) -> bool {
    for i in 1..cells.len() - 1 {
        if cells[i].class != config.firing.class {
            return false;
        }
    }
    true
}

fn new_cells<'a>(n: usize, config: &'a Config) -> Vec<&'a State<'a>> {
    let mut cells: Vec<&'a State<'a>> = vec![config.soldier; n + 2];
    cells[0] = config.external;
    let i = cells.len() - 1;
    cells[i] = config.external;
    return cells;
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

    for (_, state) in &config.states {
        match state.class {
            "general" => config.general = &state,
            "soldier" => config.soldier = &state,
            "external" => config.external = &state,
            "firing" => config.firing = &state,
            _ => {}
        }
    }

    let mut env = Env { config: &config };

    let mut current = new_cells(cell_size, &config);
    let mut next = new_cells(cell_size, &config);

    current[1] = config.general;

    let start = Instant::now();
    // env.dump(&current);

    while !fired(&current, &config) {
        env.next_lines(&current, &mut next);
        (current, next) = (next, current);
        // env.dump(&current);
    }

    let end = start.elapsed();

    println!(
        "fired: {}.{:03}s",
        end.as_secs(),
        end.subsec_nanos() / 1_000_000
    );
}
