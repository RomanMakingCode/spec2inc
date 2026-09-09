"""Agentic optimization loop for reduction_engine.

The design agent is asked to cut the logic depth of the reduction datapath
without changing what it computes. It gets one tool -- rewrite the RTL file --
and nothing else. The loop runs the frozen testbench and the synthesis gate
itself and feeds the numbers back, so every reported improvement is one the loop
measured rather than one the agent claimed.

Correctness is not negotiable and not the agent's to define: the testbench and
the reference model behind it are outside the agent's reach, so a candidate that
breaks behavior fails regardless of how much depth it saves.

Results land in git rather than in a scratch directory. The loop branches off
the current HEAD, edits the real RTL in place, and commits every attempt with
its measurements in the message. Nothing has to be "adopted" -- the work is
already versioned, and a run you dislike is a branch you delete. Your original
branch is restored on the way out, so main is never touched.

    export THIS_MACHINE=$(hostname -I | awk '{print $1}')
    export GOOGLE_CLOUD_PROJECT=<project id>
    chia up -y examples/agent_opt/cluster.yaml
    python -u examples/agent_opt/loop.py --attempts 6
    chia down -y examples/agent_opt/cluster.yaml

Afterwards:

    git log --oneline main..agent/reduction_engine-<stamp>   # what it tried
    git diff main..agent/reduction_engine-<stamp>            # the net change
    git merge agent/reduction_engine-<stamp>                 # keep it
    git branch -D agent/reduction_engine-<stamp>             # or don't
"""

import argparse
import json
import os
import subprocess
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
TARGET = REPO / "rtl" / "reduction_engine.sv"
LOGS_DIR = REPO / ".agent_runs"

# The parameter point the agent optimizes for. 16 ports is mid-range: big
# enough that the chain is clearly worse than a tree, small enough to simulate
# quickly. Improvements are re-checked at other points at the end.
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
"""


# --------------------------------------------------------------------- git


def git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=REPO,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout.strip()


def require_clean_tree() -> None:
    """Refuse to start on a dirty tree.

    The agent edits tracked files in place, so pre-existing uncommitted work
    would end up mixed into its commits and be impossible to separate later.
    """
    dirty = git("status", "--porcelain")
    if dirty:
        raise SystemExit(
            "working tree has uncommitted changes; commit or stash first:\n"
            + dirty
        )


def commit_attempt(attempt: int, ev: Evaluation, accepted: bool,
                   model: str) -> str:
    """Commit whatever the agent just wrote, accepted or not.

    Rejected attempts are committed too: the sequence of things that did not
    work is most of what a run has to say, and dropping it would leave the same
    evidence gap that scratch directories did.
    """
    if not git("status", "--porcelain", "--", str(TARGET)):
        return ""  # agent changed nothing

    if ev.usable:
        headline = (f"depth={ev.synth.depth} cells={ev.synth.cells} "
                    f"({'accepted' if accepted else 'no improvement'})")
    elif ev.test.passed:
        headline = "rejected: synthesis failed"
    else:
        headline = f"rejected: {ev.test.summary()}"

    body = [
        f"tests: {ev.test.summary()}",
        f"point: N_PORTS={ev.n_ports} RED_LATENCY={ev.red_latency}",
        f"model: {model}",
    ]
    if ev.synth is not None:
        body.append(f"synth: {ev.synth.summary()}")

    git("add", "--", str(TARGET))
    git("commit", "-m",
        f"agent(reduction_engine): attempt {attempt} -- {headline}",
        "-m", "\n".join(body),
        "-m", "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>")
    return git("rev-parse", "--short", "HEAD")


# ---------------------------------------------------------------- the loop


def evaluate(ev: EvalNode, n_ports: int, red_latency: int) -> Evaluation:
    test = get(ev.run_tests.chia_remote(ev, str(REPO), n_ports, red_latency))
    synth = None
    if test.passed:
        # Only worth synthesizing something that works.
        synth = get(ev.run_synth.chia_remote(ev, str(REPO), n_ports))
    return Evaluation(n_ports=n_ports, red_latency=red_latency,
                      test=test, synth=synth)


def format_feedback(attempt: int, ev: Evaluation, best_depth: int) -> str:
    if not ev.test.passed:
        tail = "\n".join(ev.test.log.strip().splitlines()[-60:])
        diagnosis = (
            "The module did not compile, so no test ran. Read the compiler "
            "error below and fix the syntax or elaboration problem."
            if ev.test.tests_run == 0 else
            "Your change altered behavior. The testbench is fixed and correct; "
            "the design is what must change."
        )
        return (
            f"Attempt {attempt} REJECTED: {ev.test.summary()}.\n\n"
            f"{diagnosis}\n\n"
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
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL",
                                                      "gemini-3.1-pro-preview"))
    ap.add_argument("--location", default=os.environ.get("GEMINI_LOCATION", "global"))
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--branch", default=None,
                    help="branch to create; default agent/reduction_engine-<stamp>")
    args = ap.parse_args()

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("GOOGLE_CLOUD_PROJECT is not set")

    require_clean_tree()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    branch = args.branch or f"agent/reduction_engine-{stamp}"
    origin_branch = git("rev-parse", "--abbrev-ref", "HEAD")
    base_commit = git("rev-parse", "--short", "HEAD")

    logs = LOGS_DIR / stamp
    logs.mkdir(parents=True, exist_ok=True)

    git("checkout", "-b", branch)
    print(f"branch {branch} off {origin_branch}@{base_commit}")

    ev = EvalNode()
    llm = None
    tool = None
    history = []
    best_depth = best_cells = None
    best_commit = base_commit

    try:
        print("\n=== baseline ===")
        base = evaluate(ev, OPT_N_PORTS, OPT_RED_LATENCY)
        if not base.usable:
            raise SystemExit(f"baseline is not usable: {base.test.summary()}")
        print(f"baseline: {base.test.summary()}, {base.synth.summary()}")

        best_depth, best_cells = base.synth.depth, base.synth.cells
        history.append({"attempt": 0, "depth": best_depth, "cells": best_cells,
                        "tests": base.test.summary(), "commit": base_commit})

        llm = VertexGeminiLLM(
            model=args.model,
            project=project,
            location=args.location,
            system_message=SYSTEM_MESSAGE,
            max_tool_iterations=12,
            retries=2,
            # CHIA defaults to 16000, which truncates here: an attempt emits the
            # whole module as a tool argument on top of the model's reasoning.
            max_tokens=args.max_tokens,
        )
        tool = RtlEditTool(name="rtl_edit", target_file=str(TARGET))
        message = TASK.format(n_tests=base.test.tests_run)

        for attempt in range(1, args.attempts + 1):
            print(f"\n=== attempt {attempt}/{args.attempts} ===")
            started = time.time()
            try:
                reply = get(llm.prompt.chia_remote(llm, message, [tool]))
            except Exception as exc:
                # One bad attempt -- truncation, a transient Vertex error, one
                # of this host's OAuth stalls -- costs one iteration, not the run.
                print(f"agent call raised {type(exc).__name__}: {str(exc)[:200]}")
                history.append({"attempt": attempt, "depth": None, "cells": None,
                                "tests": f"agent error: {type(exc).__name__}",
                                "commit": ""})
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
                print("agent call failed; continuing")
                continue

            cand = evaluate(ev, OPT_N_PORTS, OPT_RED_LATENCY)
            improved = cand.usable and cand.synth.depth < best_depth

            if cand.usable:
                print(f"  {cand.test.summary()}, {cand.synth.summary()}"
                      + ("  <- new best" if improved else ""))
            else:
                reason = "tests" if not cand.test.passed else "synthesis"
                print(f"  {cand.test.summary()} -- rejected on {reason}")
                log = (cand.test.log if not cand.test.passed
                       else cand.synth.log)
                (logs / f"attempt_{attempt}_{reason}.log").write_text(log)

            sha = commit_attempt(attempt, cand, improved, args.model)
            if sha:
                print(f"  committed {sha}")

            history.append({
                "attempt": attempt,
                "depth": cand.synth.depth if cand.usable else None,
                "cells": cand.synth.cells if cand.usable else None,
                "tests": cand.test.summary(),
                "commit": sha,
            })

            if improved:
                best_depth, best_cells = cand.synth.depth, cand.synth.cells
                best_commit = sha

            message = format_feedback(attempt, cand, best_depth)
    finally:
        if tool is not None:
            tool.stop()
        # Any straggler edit from a crashed attempt still gets committed, so
        # checkout below cannot fail on a dirty tree and nothing is lost.
        if git("status", "--porcelain", "--", str(TARGET)):
            git("add", "--", str(TARGET))
            git("commit", "-m", "agent(reduction_engine): uncommitted edit at exit")

    # Leave the branch tip at the best design rather than at whatever the last
    # attempt happened to produce, so `git diff <origin>..<branch>` is the
    # result and nothing has to be hand-picked out of the history. When no
    # attempt improved on the baseline this restores the original file, making
    # the net diff empty while the attempt history survives on the branch.
    if best_commit != git("rev-parse", "--short", "HEAD"):
        git("checkout", best_commit, "--", str(TARGET))
        if git("status", "--porcelain"):
            git("commit", "-m",
                f"agent(reduction_engine): restore best design from {best_commit}")
            print(f"\nrestored best design from {best_commit}")

    print("\n=== result ===")
    print(f"depth {history[0]['depth']} -> {best_depth}   "
          f"cells {history[0]['cells']} -> {best_cells}")

    verified = []
    if best_depth < history[0]["depth"]:
        print("\nre-checking the winning design at other parameter points:")
        for n_ports, red_latency in VERIFY_POINTS:
            chk = evaluate(ev, n_ports, red_latency)
            print(f"  N_PORTS={n_ports} RED_LATENCY={red_latency}: "
                  f"{chk.test.summary()}"
                  + (f", {chk.synth.summary()}" if chk.usable else ""))
            verified.append({
                "n_ports": n_ports, "red_latency": red_latency,
                "passed": chk.test.passed,
                "depth": chk.synth.depth if chk.usable else None,
                "cells": chk.synth.cells if chk.usable else None,
            })

    (logs / "summary.json").write_text(json.dumps({
        "model": args.model, "branch": branch, "base": base_commit,
        "baseline": history[0], "history": history,
        "best_depth": best_depth, "best_cells": best_cells,
        "verified": verified,
    }, indent=2))

    git("checkout", origin_branch)
    print(f"\nback on {origin_branch}; the run is on {branch}")
    print(f"  git log --oneline {origin_branch}..{branch}")
    print(f"  git diff {origin_branch}..{branch}")
    print(f"  git merge {branch}      # keep it")
    print(f"  git branch -D {branch}  # or don't")


if __name__ == "__main__":
    main()
