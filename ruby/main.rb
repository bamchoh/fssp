require 'benchmark'
require 'pp'
require "optparse"
require_relative 'simulator.rb'

dump_mode = false
opts = OptionParser.new
opts.on("-d", "--dump", "dump cells for each line") { dump_mode = true }
opts.parse!(ARGV)

if ARGV.size < 1
	puts "Please specify cell size"
	exit(-1)
end

states, rules = parse_file("../waksman-slim.rul.txt")

time = Benchmark.realtime {
	simulator = Simulator.new(states, rules, ARGV[0].to_i)

	if dump_mode
		puts simulator.dump

		while(!simulator.next) do
			puts simulator.dump
		end

		puts simulator.dump
	else
		while(!simulator.next) do
		end
	end
}

printf("%.4f s\n", time)