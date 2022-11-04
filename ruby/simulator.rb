class Simulator
	def initialize(states, rules, num)
		@states = states
		@rules = rules
		@cur_line = create_line(num)
		@nxt_line = create_line(num)
		@fired = @states.find_index { _1.type == "firing"}
	end

	def create_line(num)
		q = @states.find_index { _1.type == "soldier" }
		g = @states.find_index { _1.type == "general" }
		w = @states.find_index { _1.type == "external" }

		cells = Array.new(num + 2, q)
		cells[0] = w
		cells[1] = g
		cells[-1] = w

		cells
	end

	def next
		j = 1
		@cur_line.each_cons(3) do |(l, c, r)|
			i = (l << 8) + (c << 4) + r
			@nxt_line[j] = @rules[i]
			j += 1
		end
		@cur_line, @nxt_line = @nxt_line, @cur_line

		fired?
	end

	def dump
		line = []
		@cur_line[1...-1].each do |c|
			line << @states[c].name
		end
		line.join("|")
	end

	def fired?
		@cur_line[1...-1].all? { _1 == @fired }
	end
end

State = Struct.new(:name, :fg_color, :bg_color, :type)

def parse_file(filename)
	state_count = nil
	states = []

	rule_count = nil
	rules = []

	File.read(filename).split("\n").each do |line|
		if !state_count.nil? && state_count > 0
			args = line.split(/[@,]/)

			states << State.new(*args)

			state_count -= 1
			next
		end

		if !rule_count.nil? && rule_count > 0
			args = line.split(/(?:##|->)/)

			l = states.find_index { _1.name == args[0] }
			c = states.find_index { _1.name == args[1] }
			r = states.find_index { _1.name == args[2] }
			i = (l << 8) + (c << 4) + r

			n = states.find_index { _1.name == args[3] }
			rules[i] = n

			rule_count -= 1
			next
		end

		case line
		when /^state_number/
			state_count = line.split(" ").last.to_i
		when /^rule_number/
			rule_count = line.split(" ").last.to_i
		end
	end

	return [states, rules]
end
