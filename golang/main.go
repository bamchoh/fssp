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

type Rule []int

type Config struct {
	States   []State
	Rules    Rule
	General  int
	Soldier  int
	Firing   int
	External int
}

type State struct {
	Name  string
	Class string
}

func parseState(scanner *bufio.Scanner, n int) []State {
	r := regexp.MustCompile("[\\s,]")
	states := make([]State, 0)
	for scanner.Scan() && n > 0 {
		info := r.Split(scanner.Text(), -1)
		s := State{info[0], info[len(info)-1]}
		states = append(states, s)
		n--
	}
	return states
}

func getIndex(states []State, cell string) int {
	for i, state := range states {
		if state.Name == cell {
			return i
		}
	}
	return -1
}

func parseRule(scanner *bufio.Scanner, n int, stateList []State) []int {
	r := regexp.MustCompile("[\\s\\#\\-\\>]")
	rules := make([]int, 4096)
	for scanner.Scan() && n > 0 {
		info := r.Split(scanner.Text(), -1)
		var states []string
		for _, state := range info {
			if state == "" {
				continue
			}
			states = append(states, state)
		}

		idx := -1

		idx = getIndex(stateList, states[0])
		idx = idx<<4 + getIndex(stateList, states[1])
		idx = idx<<4 + getIndex(stateList, states[2])
		rules[idx] = getIndex(stateList, states[3])

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

			for i, state := range config.States {
				switch state.Class {
				case "general":
					config.General = i
				case "soldier":
					config.Soldier = i
				case "external":
					config.External = i
				case "firing":
					config.Firing = i
				}
			}
		}

		if strings.HasPrefix(scanner.Text(), "rule_number") {
			nText := strings.Split(scanner.Text(), " ")[1]
			n, err := strconv.Atoi(nText)
			if err != nil {
				panic(err)
			}
			config.Rules = parseRule(scanner, n, config.States)
		}
	}
	return config
}

func firstline(config Config, size int) []int {
	cells := newline(config, size)
	cells[1] = config.General
	return cells
}

func newline(config Config, size int) []int {
	cells := make([]int, size+2)
	for i := 1; i < len(cells)-1; i++ {
		cells[i] = config.Soldier
	}
	cells[0] = config.External
	cells[len(cells)-1] = config.External
	return cells
}

func dump(cells []int, config Config) {
	fmt.Print("|")
	for i := 1; i < len(cells)-1; i++ {
		fmt.Printf("%4v|", config.States[cells[i]].Name)
	}
	fmt.Println()
}

func firing(cells []int, config Config) bool {
	for i := 1; i < len(cells)-1; i++ {
		if cells[i] != config.Firing {
			return false
		}
	}
	return true
}

func nextState(cells []int, config Config) []int {
	nextCells := newline(config, len(cells)-2)
	for i := 1; i < len(cells)-1; i++ {
		left := cells[i-1]
		center := cells[i]
		right := cells[i+1]
		key := (left << 8) + (center << 4) + right
		nextCells[i] = config.Rules[key]
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

	dump(cells, config)

	for !firing(cells, config) {
		cells = nextState(cells, config)
		dump(cells, config)
	}

	fmt.Printf("firied: %v", time.Since(now).Seconds())
}
