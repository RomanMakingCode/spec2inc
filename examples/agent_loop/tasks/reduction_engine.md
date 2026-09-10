Optimize the SystemVerilog module `reduction_engine` for **logic depth**.

## What it must keep doing

It reduces up to N_PORTS operand beats into one. Each beat is DATA_W=256 bits,
split into RED_LANES=8 independent lanes of RED_LANE_W=32 bits. For every lane
position, the output lane is the sum of that lane across all operands whose bit
is set in `op_mask`. Lanes are independent: a carry out of one lane must never
reach the next. Addition wraps at 2**32; there is no saturation.

Operands live in the flat `op_data` vector: operand p occupies
`op_data[p*DATA_W +: DATA_W]`, and lane l within a beat occupies
`[l*RED_LANE_W +: RED_LANE_W]`.

An unset mask bit contributes nothing. A mask with one bit set therefore returns
that operand unchanged. The result must not depend on which slots the operands
occupy, only on the set of them.

The module has a fixed-latency pipeline: a result appears exactly RED_LATENCY
cycles after acceptance, `op_id` is carried through to `res_id` untouched, and
the whole pipeline freezes while a completed result waits for `res_ready`.

## Hard constraints

- Do NOT change the port list, the parameters, or the module name.
- Do NOT change the observable latency: still exactly RED_LATENCY cycles.
- Do NOT change the ready/valid behavior, including the freeze-on-backpressure.
- The design must survive `sv2v` conversion followed by `yosys synth`. It is
  checked, and a design that simulates but does not synthesize is rejected.
  Write ordinary synthesizable RTL: `always_comb` / `always_ff`, `genvar`
  generate loops, packed vectors. Avoid `$bits()` applied to a *type*, dynamic
  or unbounded array types, `initial` blocks, and anything else that only makes
  sense in simulation.

## Where the depth actually goes

Measured with yosys `ltp` on the current design, logic depth is 86 at
N_PORTS=8, 111 at 16, and 255 at 64 -- it grows linearly with N_PORTS because
the reduction is a sequential accumulation across all operands in one
`always_comb`.

Be aware of the floor: roughly 60 levels of the total is carry propagation
through a single 32-bit add, which is paid once no matter how the operands are
combined, and only a few levels come from each additional combining step. So
restructuring how operands are combined helps a lot, while merely moving those
combining steps across pipeline registers does not -- the carry chain stays
wherever the final add lands. Getting below the floor needs the addition itself
restructured, for example carry-save accumulation with one carry-propagate at
the end.

## How you are measured

`logic_depth` (yosys longest topological path) is the objective; lower is
better. `cells` is reported too and should not blow up. All {n_tests} tests of
the existing testbench must keep passing -- you cannot see or change that
testbench, and a design that fails it is rejected no matter how shallow it is.

## How to work

Call the read tool to see the current module, then the write tool with the
complete new file. Make one substantive change per attempt so the measurement
attributes cleanly.
