package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Rule map[string]map[string]map[string]string

type Config struct {
	States   map[string]State
	Rules    Rule
	General  State
	Soldier  State
	Firing   State
	External State
}

type State struct {
	Name  string
	Class string
}

func parseState(scanner *bufio.Scanner, n int) map[string]State {
	r := regexp.MustCompile("[\\s,]")
	states := make(map[string]State, 0)
	for scanner.Scan() && n > 0 {
		info := r.Split(scanner.Text(), -1)
		s := State{info[0], info[len(info)-1]}
		states[info[0]] = s
		n--
	}
	return states
}

func parseRule(scanner *bufio.Scanner, n int) Rule {
	r := regexp.MustCompile("[\\s\\#\\-\\>]")
	rules := make(Rule, 0)
	for scanner.Scan() && n > 0 {
		info := r.Split(scanner.Text(), -1)
		var states []string
		for _, state := range info {
			if state == "" {
				continue
			}
			states = append(states, state)
		}

		left := states[0]
		center := states[1]
		right := states[2]
		next := states[3]

		if _, ok := rules[left]; !ok {
			rules[left] = make(map[string]map[string]string)
		}

		cMap := rules[left]
		if _, ok := cMap[center]; !ok {
			cMap[center] = make(map[string]string)
		}

		rMap := cMap[center]
		if _, ok := rMap[right]; !ok {
			rMap[right] = next
		}

		n--
	}
	return rules
}

func parseRuleFile(fp io.Reader) Config {
	scanner := bufio.NewScanner(fp)
	var config Config
	for scanner.Scan() {
		if strings.HasPrefix(scanner.Text(), "state_number") {
			nText := strings.Split(scanner.Text(), " ")[1]
			n, err := strconv.Atoi(nText)
			if err != nil {
				panic(err)
			}
			config.States = parseState(scanner, n)

			for _, state := range config.States {
				switch state.Class {
				case "general":
					config.General = state
				case "soldier":
					config.Soldier = state
				case "external":
					config.External = state
				case "firing":
					config.Firing = state
				}
			}
		}

		if strings.HasPrefix(scanner.Text(), "rule_number") {
			nText := strings.Split(scanner.Text(), " ")[1]
			n, err := strconv.Atoi(nText)
			if err != nil {
				panic(err)
			}
			config.Rules = parseRule(scanner, n)
		}
	}
	return config
}

func firstline(config Config, size int) []string {
	cells := newline(config, size)
	cells[1] = config.General.Name
	return cells
}

func newline(config Config, size int) []string {
	cells := make([]string, size+2)
	for i := 1; i < len(cells)-1; i++ {
		cells[i] = config.Soldier.Name
	}
	cells[0] = config.External.Name
	cells[len(cells)-1] = config.External.Name
	return cells
}

func dump(cells []string) {
	fmt.Print("|")
	for i := 1; i < len(cells)-1; i++ {
		fmt.Printf("%4v|", cells[i])
	}
	fmt.Println()
}

func firing(cells []string, config Config) bool {
	for i := 1; i < len(cells)-1; i++ {
		if cells[i] != config.Firing.Name {
			return false
		}
	}
	return true
}

func nextState(cells []string, config Config) []string {
	nextCells := newline(config, len(cells)-2)
	for i := 1; i < len(cells)-1; i++ {
		left := cells[i-1]
		if cMap, ok := config.Rules[left]; ok {
			center := cells[i]
			if rMap, ok := cMap[center]; ok {
				right := cells[i+1]
				if nextState, ok := rMap[right]; ok {
					nextCells[i] = nextState
				}
			}
		}
	}
	return nextCells
}

func main() {
	var err error
	var size int
	if len(os.Args) < 2 {
		size = 10
	} else {
		size, err = strconv.Atoi(os.Args[1])
		if err != nil {
			panic(err)
		}
	}

	fp, err := os.Open("../waksman-slim.rul.txt")
	if err != nil {
		panic(err)
	}
	defer fp.Close()

	config := parseRuleFile(fp)

	cells := firstline(config, size)

	now := time.Now()

	// dump(cells)

	for !firing(cells, config) {
		cells = nextState(cells, config)
		// dump(cells)
	}

	fmt.Printf("firied: %v", time.Since(now).Seconds())
}
