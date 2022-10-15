package main

import (
	"bufio"
	"fmt"
	"io"
	"math"
	"os"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
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

func nextState(cur []int, nex []int, config Config) ([]int, []int) {
	for i := 1; i < len(cur)-1; i++ {
		nex[i] = config.Rules[(cur[i-1]<<8)+(cur[i]<<4)+cur[i+1]]
	}
	return nex, cur
}

func simulate(cur []int, nex []int, config Config) {
	// dump(cur, config)
	for !firing(cur, config) {
		cur, nex = nextState(cur, nex, config)
		// dump(cur, config)
	}
}

type AryPair struct {
	Cur         []int
	Nex         []int
	ToLeftCh    chan int
	FromLeftCh  chan int
	ToRightCh   chan int
	FromRightCh chan int
}

func split(cur []int, nex []int, n int) []AryPair {
	ary_len := len(cur)
	n = int(math.Ceil(float64(ary_len) / float64(n)))
	idx_ofs := 0
	var splitted_ary []AryPair

	for idx_ofs < ary_len {
		var start_idx int
		var size_ofs int
		if idx_ofs == 0 {
			start_idx, size_ofs = idx_ofs, 1
		} else {
			start_idx, size_ofs = idx_ofs-1, 2
		}

		var size int
		if n+size_ofs+start_idx < ary_len {
			size = n + size_ofs
		} else {
			size = ary_len - start_idx
		}

		splitted_ary = append(splitted_ary, AryPair{
			cur[start_idx : start_idx+size],
			nex[start_idx : start_idx+size],
			nil, nil, nil, nil,
		})

		idx_ofs += n
	}

	for i := 1; i < len(splitted_ary); i++ {
		Ch1 := make(chan int, 1)
		splitted_ary[i-1].ToRightCh = Ch1
		splitted_ary[i].FromLeftCh = Ch1

		Ch2 := make(chan int, 1)
		splitted_ary[i].ToLeftCh = Ch2
		splitted_ary[i-1].FromRightCh = Ch2
	}

	return splitted_ary
}

func parNextState(aryPair AryPair, config Config, wg *sync.WaitGroup) {
	defer wg.Done()
	t := 0
	for !firing(aryPair.Cur, config) {
		aryPair.Cur, aryPair.Nex = nextState(aryPair.Cur, aryPair.Nex, config)
		// dump(cur, config)

		if aryPair.ToLeftCh != nil {
			aryPair.ToLeftCh <- 1
		}

		if aryPair.ToRightCh != nil {
			aryPair.ToRightCh <- 1
		}

		if aryPair.FromLeftCh != nil {
			<-aryPair.FromLeftCh
		}

		if aryPair.FromRightCh != nil {
			<-aryPair.FromRightCh
		}

		t++
	}
}

func parSimulate(cur []int, nex []int, config Config) {
	var wg sync.WaitGroup

	cpus := runtime.NumCPU()

	aryPairs := split(cur, nex, cpus)

	for _, aryPair := range aryPairs {
		wg.Add(1)
		go parNextState(aryPair, config, &wg)
	}

	wg.Wait()

	// dump(cur, config)
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

	cur := firstline(config, size)
	nex := newline(config, len(cur)-2)

	now := time.Now()

	parSimulate(cur, nex, config)

	fmt.Printf("firied: %v", time.Since(now).Seconds())
}
