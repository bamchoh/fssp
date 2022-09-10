use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::{BufReader, Read};

#[derive(Debug)]
struct Config<'a> {
    states: HashMap<&'a str, State<'a>>,
    general: &'a State<'a>,
    soldier: &'a State<'a>,
    firing: &'a State<'a>,
    external: &'a State<'a>,
    rules: Vec<Rule>,
}

struct Env<'a> {
    config: &'a Config<'a>,
    current: Vec<&'a State<'a>>,
}

impl<'a> Env<'a> {
    fn dump(&self) {
        print!("|");
        for i in 1..self.current.len() - 1 {
            print!("{0: <4}|", self.current[i].name);
        }
        println!();
    }

    fn next_lines(&mut self) {
        let mut next_cells = new_cells(self.current.len() - 2, self.config);

        for i in 1..self.current.len() - 1 {
            let left = self.current[i - 1].name;
            let center = self.current[i].name;
            let right = self.current[i + 1].name;

            for rule in &self.config.rules {
                if rule.left == left && rule.center == center && rule.right == right {
                    match self.config.states.get(&rule.next.as_str()) {
                        Some(n) => next_cells[i] = n,
                        _ => {}
                    }
                }
            }
        }

        self.current = next_cells;
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
struct Rule {
    left: String,
    center: String,
    right: String,
    next: String,
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
        left: String::from(v[0]),
        center: String::from(v[1]),
        right: String::from(v[2]),
        next: String::from(v[3]),
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
        rules: vec![],
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
                config.rules.push(rule);
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

    println!("{:?}", args);

    if args.len() < 2 {
        cell_size = 10;
    } else {
        cell_size = args[1].parse().unwrap();
    }

    let s = match read_file("./waksman-slim.rul.txt".to_owned()) {
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

    let mut env = Env {
        config: &config,
        current: new_cells(cell_size, &config),
    };

    env.current[1] = config.general;

    env.dump();

    while !fired(&env.current, &config) {
        env.next_lines();
        env.dump();
    }

    println!("fired");
}
