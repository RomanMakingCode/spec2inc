Implement the SystemVerilog module `group_table`. It is currently a stub: the
port list is correct and the body does nothing, so every test fails.

## What it must do

It stores group membership for one switch port. A "group" is a set of
accelerators participating in a collective, identified by a GroupID. Each entry
is a valid bit plus a bitmask, one bit per accelerator, `MAX_PORTS` wide.

The table holds `GROUP_TABLE_ENTRIES` entries, indexed by `wr_idx` / `rd_idx`,
which are `ID_W` bits wide. Note that `ID_W` covers 64 indices while
`GROUP_TABLE_ENTRIES` may be smaller, so the index space can be larger than the
table.

## Contract

- **Reads are combinational.** `rd_valid_bit` and `rd_members` reflect the
  entry selected by `rd_idx` in the same cycle, with no register in between.
  The surrounding logic classifies a request and resolves its group in a single
  cycle, so a registered read would not fit.
- **Writes are synchronous.** A write takes effect on the clock edge where
  `wr_en` is high. A read of that same index during the write cycle still
  returns the old entry; the new one appears the cycle after.
- Read-after-write returns exactly what was written, valid bit and mask.
- Entries are independent. Writing one must not disturb any other.
- An `rd_idx` at or beyond `GROUP_TABLE_ENTRIES` reads as invalid:
  `rd_valid_bit` low and `rd_members` zero. It must not alias onto a real
  entry.
- Reset clears every valid bit. It need not clear the masks -- a clear valid
  bit already means the entry carries nothing.
- Writing an entry with `wr_valid_bit` low makes that entry read invalid.

## Hard constraints

- Do NOT change the port list, the parameters, or the module name.
- Storage must be an explicit register array, never an associative array or
  any other dynamically sized type.
- The design must survive `sv2v` conversion followed by `yosys synth`. It is
  checked, and a design that simulates but does not synthesize is rejected.
  Write ordinary synthesizable RTL: `always_comb` / `always_ff`, `genvar`
  generate loops, packed vectors. Avoid `$bits()` applied to a *type*, dynamic
  or unbounded array types, `initial` blocks, and anything else that only makes
  sense in simulation.

## How you are measured

A hidden testbench checks every bullet above. You cannot see or change it. The
module must also synthesize. There is no performance target here -- passing is
the whole goal.

## How to work

Call the read tool to see the current file, then the write tool with the
complete new file.
