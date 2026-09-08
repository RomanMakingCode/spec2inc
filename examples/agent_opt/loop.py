"""Agentic optimization loop for reduction_engine.

The design agent is asked to cut the logic depth of the reduction datapath
without changing what it computes. It gets one tool -- rewrite the RTL file --
and nothing else. The loop runs the frozen testbench and the synthesis gate
itself and feeds the numbers back, so every reported improvement is one the loop
measured rather than one the agent claimed.

Correctness is not negotiable and not the agent's to define: the testbench and
the reference model behind it are outside the agent's reach, so a candidate that
breaks behavior fails regardless of how much depth it saves.

    export THIS_MACHINE=$(hostname -I | awk '{print $1}')
    export GOOGLE_CLOUD_PROJECT=<project id>
    chia up -y examples/agent_opt/cluster.yaml
    python examples/agent_opt/loop.py --attempts 4
    chia down -y examples/agent_opt/cluster.yaml
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.base.ChiaFunction import get                      # noqa: E402
from chia.models.vertex import VertexGeminiLLM              # noqa: E402
from nodes import EvalNode, Evaluation                      # noqa: E402
from tools import RtlEditTool                               # noqa: E402

REPO = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO / ".agent_runs"

# The parameter point the agent optimizes for. 16 ports is mid-range: big
# enough that the chain is clearly worse than a tree, small enough to simulate
# quickly. Improvements are re-checked at 8 and 64 at the end.
OPT_N_PORTS = 16
OPT_RED_LATENCY = 3
VERIFY_POINTS = [(8, 1), (8, 3), (16, 1), (16, 6), (64, 3)]

SYSTEM_MESSAGE = """You are a senior RTL designer working in SystemVerilog.
You make focused, correct changes and you do not guess: if you are unsure whether
a rewrite preserves behavior, you choose the version you can justify.
You never change a module's port list or parameters."""

TASK = """\
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

## Why it is currently slow

The reduction is written as a sequential accumulation across all N_PORTS
operands inside a single `always_comb`. That is a chain of N adders, so logic
depth grows linearly with N_PORTS. Measured depth is 86 levels at N_PORTS=8,
111 at 16, and 255 at 64.

A balanced adder tree would make depth grow with log2(N_PORTS) instead. Note
that RED_LATENCY registers already exist in the pipeline -- distributing tree
levels across them is allowed and encouraged, as long as total latency stays
exactly RED_LATENCY cycles.

## How you are measured

`logic_depth` (yosys longest topological path) is the objective; lower is
better. `cells` is reported too and should not blow up. All {n_tests} tests of
the existing testbench must keep passing -- you cannot see or change that
testbench, and a design that fails it is rejected no matter how shallow it is.

## How to work

Call the read tool to see the current module, then the write tool with the
complete new file. Make one substantive change per attempt so the measurement
attributes cleanly.
"""


def make_workdir() -> Path:
    """Copy the RTL and verification sources somewhere the agent can scribble.

    The agent never edits the repo's own files: a run that goes badly should
    leave nothing behind but a directory under .agent_runs/.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work = RUNS_DIR / stamp
    (work / "rtl").mkdir(parents=True, exist_ok=True)
    (work / "verif" / "reduction_engine").mkdir(parents=True, exist_ok=True)

    for name in ("spec2inc_pkg.sv", "reduction_engine.sv"):
        shutil.copy2(REPO / "rtl" / name, work / "rtl" / name)
    shutil.copy2(REPO / "verif" / "reference.py", work / "verif" / "reference.py")
    for name in ("Makefile", "tb_reduction_engine.py"):
        shutil.copy2(REPO / "verif" / "reduction_engine" / name,
                     work / "verif" / "reduction_engine" / name)
    return work


def evaluate(ev: EvalNode, work: Path, n_ports: int, red_latency: int) -> Evaluation:
    test = get(ev.run_tests.chia_remote(ev, str(work), n_ports, red_latency))
    synth = None
    if test.passed:
        # Only worth synthesizing something that works.
        synth = get(ev.run_synth.chia_remote(ev, str(work), n_ports))
    return Evaluation(n_ports=n_ports, red_latency=red_latency, test=test, synth=synth)


def format_feedback(attempt: int, ev: Evaluation, best_depth: int) -> str:
    if not ev.test.passed:
        tail = "\n".join(ev.test.log.strip().splitlines()[-60:])
        return (
            f"Attempt {attempt} REJECTED: {ev.test.summary()}.\n\n"
            "Your change altered behavior or broke the build. The testbench is "
            "fixed and correct; the design is what must change. Relevant output:\n\n"
            f"```\n{tail}\n```\n\n"
            "Fix this. Correctness comes before depth."
        )
    if not ev.synth.ok:
        tail = "\n".join(ev.synth.log.strip().splitlines()[-40:])
        return (
            f"Attempt {attempt} REJECTED: tests pass but synthesis failed.\n\n"
            f"```\n{tail}\n```\n\nMake it elaborate under sv2v and yosys."
        )

    verdict = (
        f"IMPROVED (best was {best_depth})"
        if ev.synth.depth < best_depth
        else f"no improvement (best is still {best_depth})"
    )
    return (
        f"Attempt {attempt} accepted: {ev.test.summary()}, "
        f"{ev.synth.summary()} -- {verdict}.\n\n"
        "Keep going: reduce logic_depth further without breaking the tests, "
        "the interface, or the exact RED_LATENCY-cycle latency."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL",
                                                      "gemini-3.1-pro-preview"))
    ap.add_argument("--location", default=os.environ.get("GEMINI_LOCATION", "global"))
    ap.add_argument("--max-tokens", type=int, default=64000)
    args = ap.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("GOOGLE_CLOUD_PROJECT is not set")

    work = make_workdir()
    target = work / "rtl" / "reduction_engine.sv"
    print(f"workdir: {work}")

    ev = EvalNode()

    print("\n=== baseline ===")
    base = evaluate(ev, work, OPT_N_PORTS, OPT_RED_LATENCY)
    if not base.usable:
        raise SystemExit(f"baseline is not usable: {base.test.summary()}")
    print(f"baseline: {base.test.summary()}, {base.synth.summary()}")

    best_depth = base.synth.depth
    best_cells = base.synth.cells
    best_src = target.read_text()
    history = [{"attempt": 0, "depth": best_depth, "cells": best_cells,
                "tests": base.test.summary()}]

    llm = VertexGeminiLLM(
        model=args.model,
        project=project,
        location=args.location,
        system_message=SYSTEM_MESSAGE,
        max_tool_iterations=12,
        retries=2,
        # CHIA defaults to 16000, which truncates here: an attempt has to emit
        # the entire ~110-line module as a tool argument on top of whatever the
        # model spends on reasoning, and a Pro model spends a lot.
        max_tokens=args.max_tokens,
    )
    tool = RtlEditTool(name="rtl_edit", target_file=str(target))

    message = TASK.format(n_tests=base.test.tests_run)

    try:
        for attempt in range(1, args.attempts + 1):
            print(f"\n=== attempt {attempt}/{args.attempts} ===")
            started = time.time()
            try:
                reply = get(llm.prompt.chia_remote(llm, message, [tool]))
            except Exception as exc:
                # A single bad attempt -- truncation, a transient Vertex error,
                # one of this host's OAuth stalls -- should cost one iteration,
                # not the whole run. Tell the agent what happened and continue.
                print(f"agent call raised {type(exc).__name__}: {str(exc)[:200]}")
                history.append({"attempt": attempt, "depth": None, "cells": None,
                                "tests": f"agent error: {type(exc).__name__}"})
                message = (
                    f"Attempt {attempt} did not complete: {type(exc).__name__}. "
                    "Your previous response was cut off before the edit landed. "
                    "Keep the reasoning brief and write the complete file in one "
                    "tool call."
                )
                continue
            print(f"agent responded in {time.time() - started:.0f}s "
                  f"(success={reply.success})")
            if not reply.success:
                print("agent call failed; continuing to next attempt")
                continue

            cand = evaluate(ev, work, OPT_N_PORTS, OPT_RED_LATENCY)

            # Snapshot every attempt before anything can overwrite it. Without
            # this a run that ends without improving leaves no trace of what the
            # agent actually tried, which is most of what there is to learn.
            snaps = work / "attempts"
            snaps.mkdir(exist_ok=True)
            (snaps / f"attempt_{attempt}.sv").write_text(target.read_text())
            if not cand.test.passed:
                (snaps / f"attempt_{attempt}_test.log").write_text(cand.test.log)
            elif cand.synth is not None and not cand.synth.ok:
                (snaps / f"attempt_{attempt}_synth.log").write_text(cand.synth.log)

            if cand.usable:
                print(f"  {cand.test.summary()}, {cand.synth.summary()}")
            else:
                reason = ("tests" if not cand.test.passed else "synthesis")
                print(f"  {cand.test.summary()} -- rejected on {reason}")

            history.append({
                "attempt": attempt,
                "depth": cand.synth.depth if cand.usable else None,
                "cells": cand.synth.cells if cand.usable else None,
                "tests": cand.test.summary(),
            })

            if cand.usable and cand.synth.depth < best_depth:
                print(f"  new best: depth {best_depth} -> {cand.synth.depth}")
                best_depth = cand.synth.depth
                best_cells = cand.synth.cells
                best_src = target.read_text()

            message = format_feedback(attempt, cand, best_depth)
    finally:
        tool.stop()

    # Restore the best design that actually passed, so the workdir holds the
    # result rather than whatever the last attempt happened to leave.
    target.write_text(best_src)

    print("\n=== result ===")
    print(f"depth {history[0]['depth']} -> {best_depth}   "
          f"cells {history[0]['cells']} -> {best_cells}")

    verified = []
    if best_depth < history[0]["depth"]:
        print("\nre-checking the winning design at other parameter points:")
        for n_ports, red_latency in VERIFY_POINTS:
            chk = evaluate(ev, work, n_ports, red_latency)
            line = (f"  N_PORTS={n_ports} RED_LATENCY={red_latency}: "
                    f"{chk.test.summary()}"
                    + (f", {chk.synth.summary()}" if chk.usable else ""))
            print(line)
            verified.append({
                "n_ports": n_ports, "red_latency": red_latency,
                "passed": chk.test.passed,
                "depth": chk.synth.depth if chk.usable else None,
                "cells": chk.synth.cells if chk.usable else None,
            })

    (work / "summary.json").write_text(json.dumps({
        "model": args.model,
        "baseline": history[0],
        "history": history,
        "best_depth": best_depth,
        "best_cells": best_cells,
        "verified": verified,
    }, indent=2))
    print(f"\nartifacts in {work}")


if __name__ == "__main__":
    main()
