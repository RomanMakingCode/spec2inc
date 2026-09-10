"""What the loop can be pointed at.

One entry per module: where its RTL lives, which testbench judges it, what
parameter points to measure, and what the agent is being asked to do. Adding a
module to the loop means adding an entry here and a task file -- not editing
the loop.

`objective` decides what counts as progress:
  "depth"       minimize yosys longest topological path (needs a working baseline)
  "cells"       minimize cell count
  "correctness" no metric to beat; passing the testbench is the whole goal
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Target:
    name: str                       # module name, and the synthesis top
    rtl: str                        # repo-relative RTL file the agent may edit
    tb_dir: str                     # repo-relative testbench directory
    task: str                       # repo-relative markdown briefing
    params: dict                    # parameter point the loop optimizes at
    objective: str = "correctness"
    verify_points: tuple = ()       # extra points re-checked once a design lands
    deps: tuple = ("rtl/spec2inc_pkg.sv",)   # other sources needed to synthesize
    skeleton: Optional[str] = None  # starting file for implement mode


TARGETS = {
    t.name: t
    for t in [
        Target(
            name="reduction_engine",
            rtl="rtl/reduction_engine.sv",
            tb_dir="verif/reduction_engine",
            task="examples/agent_loop/tasks/reduction_engine.md",
            # 16 ports is mid-range: wide enough that a chain is clearly worse
            # than a tree, narrow enough to simulate quickly.
            params={"N_PORTS": 16, "RED_LATENCY": 3},
            objective="depth",
            verify_points=(
                {"N_PORTS": 8, "RED_LATENCY": 1},
                {"N_PORTS": 8, "RED_LATENCY": 3},
                {"N_PORTS": 16, "RED_LATENCY": 1},
                {"N_PORTS": 16, "RED_LATENCY": 6},
                {"N_PORTS": 64, "RED_LATENCY": 3},
            ),
        ),
        Target(
            name="group_table",
            rtl="rtl/group_table.sv",
            tb_dir="verif/group_table",
            task="examples/agent_loop/tasks/group_table.md",
            params={"N_PORTS": 16, "GROUP_TABLE_ENTRIES": 64},
            objective="correctness",
            # A smaller table exercises the out-of-range path, which is
            # unreachable when the table covers the whole index space.
            verify_points=(
                {"N_PORTS": 8, "GROUP_TABLE_ENTRIES": 16},
                {"N_PORTS": 32, "GROUP_TABLE_ENTRIES": 4},
                {"N_PORTS": 64, "GROUP_TABLE_ENTRIES": 64},
            ),
            skeleton="examples/agent_loop/skeletons/group_table.sv",
        ),
    ]
}
