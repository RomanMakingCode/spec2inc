Implement the SystemVerilog module `req_xbar`. It is currently a stub: the port
list is correct and the body does nothing, so every test fails.

## What it must do

Route request beats from any source to the sink named by the beat's `dst`
field, arbitrating when several sources want the same sink at once.

There are `N_PORTS + 2` sources. Indices 0 to `N_PORTS-1` are the per-port
unicast streams; index `N_PORTS` is the primitive engine and `N_PORTS+1` the
block engine. There are `N_PORTS` sinks, one per port egress. Sources and sinks
are flattened: element *i* occupies `[i*REQ_W +: REQ_W]`, and a beat's fields
follow `req_t` in `spec2inc_pkg`.

All channels use ready/valid: a transfer happens on the cycle where both are
high. `ready` must not depend combinationally on the `valid` of the same
channel, or the fabric can close a combinational loop through itself.

## Contract

- A beat presented at a source with `dst == d` is delivered to sink *d* exactly
  once: never dropped, never duplicated, never misrouted, never altered.
- Beats within one source-to-sink flow keep their order. There is no ordering
  guarantee between different flows.
- A multi-beat transfer occupies its sink until the beat with `last` set. No
  other source may interleave beats into that sink meanwhile, or the egress
  cannot tell the two transfers apart.
- No source is starved indefinitely. Under continuous contention every source
  must make progress -- draining one source completely before serving another
  is not acceptable.
- A sink holding `m_ready` low accepts nothing and loses nothing: its traffic
  waits rather than being discarded, and traffic to other sinks keeps flowing.

## Hard constraints

- Do NOT change the port list, the parameters, or the module name.
- The design must survive `sv2v` conversion followed by `yosys synth`. It is
  checked, and a design that simulates but does not synthesize is rejected.
  Write ordinary synthesizable RTL: `always_comb` / `always_ff`, `genvar`
  generate loops, packed vectors. Avoid `$bits()` applied to a *type*, dynamic
  or unbounded array types, `initial` blocks, and anything else that only makes
  sense in simulation.

## How you are measured

A hidden testbench checks every bullet above. You cannot see or change it. The
module must also synthesize. There is no performance target -- passing is the
whole goal.

## How to work

Call the read tool to see the current file, then the write tool with the
complete new file.

Replace the file's header comment as part of implementing it. The one there now
describes an empty interface, and leaving it in place would make the finished
module misdescribe itself.
