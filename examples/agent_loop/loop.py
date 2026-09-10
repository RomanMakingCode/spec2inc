"""Agent loop over any module in the target registry.

Two modes, same machinery:

  implement   the module is a stub; the agent writes it against a testbench it
              cannot see. Success is the testbench passing.
  optimize    the module works; the agent improves a measured objective without
              breaking it. Success is beating the baseline.

In both, the agent gets one tool -- rewrite the RTL file -- and the loop owns
evaluation, running the frozen testbench and the synthesis gate itself and
feeding the numbers back. Correctness is not the agent's to define: the
testbench and the reference model behind it are outside its reach, so a
candidate that breaks behavior fails regardless of what else it achieves.

Results land in git rather than a scratch directory. The loop branches off
HEAD, edits the real RTL in place, and commits every attempt with its
measurements. Nothing has to be adopted -- the work is already versioned, and a
run you dislike is a branch you delete. Your original branch is restored on the
way out, so main is never touched.

    export THIS_MACHINE=$(hostname -I | awk '{print $1}')
    export GOOGLE_CLOUD_PROJECT=<project id>
    chia up -y examples/agent_loop/cluster.yaml
    python -u examples/agent_loop/loop.py group_table --mode implement
    python -u examples/agent_loop/loop.py reduction_engine --mode optimize
    chia down -y examples/agent_loop/cluster.yaml
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chia.base.ChiaFunction import get                      # noqa: E402
from chia.models.vertex import VertexGeminiLLM              # noqa: E402
from nodes import EvalNode, Evaluation                      # noqa: E402
from targets import TARGETS, Target                         # noqa: E402
from tools import RtlEditTool                               # noqa: E402

REPO = Path(__file__).resolve().parents[2]
LOGS_DIR = REPO / ".agent_runs"

SYSTEM_MESSAGE = """You are a senior RTL designer working in SystemVerilog.
You make focused, correct changes and you do not guess: if you are unsure whether
a rewrite preserves behavior, you choose the version you can justify.
You never change a module's port list or parameters."""


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
            "working tree has uncommitted changes; commit or stash first:\n" + dirty
        )


def commit(target: Target, subject: str, body: list) -> str:
    """Commit the module file if it changed, returning the short sha."""
    if not git("status", "--porcelain", "--", target.rtl):
        return ""
    git("add", "--", target.rtl)
    git("commit", "-m", subject, "-m", "\n".join(body),
        "-m", "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>")
    return git("rev-parse", "--short", "HEAD")


# ---------------------------------------------------------------- the loop


def evaluate(ev: EvalNode, target: Target, params: dict) -> Evaluation:
    test = get(ev.run_tests.chia_remote(ev, str(REPO), target.tb_dir, params))
    synth = None
    if test.passed:
        # Only worth synthesizing something that works.
        sources = list(target.deps) + [target.rtl]
        synth = get(ev.run_synth.chia_remote(
            ev, str(REPO), sources, target.name, params))
    return Evaluation(params=params, test=test, synth=synth)


def check_verify_points(ev_node: EvalNode, target: Target):
    """Evaluate the secondary parameter points, stopping at the first failure.

    Run inside the attempt loop rather than once at the end. A module that
    works at one parameter point and does not build at another is not finished,
    and the agent can only fix what it is told about -- reporting it after the
    loop has already declared success wastes the attempt that would have fixed
    it.

    Only called once the primary point passes, so the cost lands on candidates
    that have earned it.
    """
    results = []
    for params in target.verify_points:
        chk = evaluate(ev_node, target, dict(params))
        results.append(chk)
        if not chk.usable:
            return results, chk
    return results, None


def score(target: Target, ev: Evaluation):
    """Lower is better. None when the candidate is not usable at all."""
    if not ev.usable:
        return None
    if target.objective == "depth":
        return ev.synth.depth
    if target.objective == "cells":
        return ev.synth.cells
    return 0  # correctness-only: every usable candidate ties


def format_feedback(target: Target, attempt: int, ev: Evaluation,
                    best, verify_fail: Evaluation = None) -> str:
    if verify_fail is not None:
        failing = verify_fail
        tail = "\n".join(
            (failing.test.log if not failing.test.passed
             else failing.synth.log).strip().splitlines()[-60:]
        )
        return (
            f"Attempt {attempt} REJECTED: it works at {ev.params} but "
            f"{'does not build' if failing.test.tests_run == 0 else 'fails'} "
            f"at {failing.params}.\n\n"
            "The module must be correct across its whole parameter range, not "
            "just at one point. Note that the index space and the table size "
            "are set by different parameters, so one can be wider than the "
            "other.\n\n"
            f"```\n{tail}\n```\n\n"
            "Fix this without breaking the point that already works."
        )

    if not ev.test.passed:
        tail = "\n".join(ev.test.log.strip().splitlines()[-60:])
        diagnosis = (
            "The module did not compile, so no test ran. Read the compiler "
            "error below and fix the syntax or elaboration problem."
            if ev.test.tests_run == 0 else
            "Your change does not match the specified behavior. The testbench "
            "is fixed and correct; the design is what must change."
        )
        return (
            f"Attempt {attempt} REJECTED: {ev.test.summary()}.\n\n{diagnosis}\n\n"
            f"```\n{tail}\n```\n\nFix this before anything else."
        )

    if not ev.synth.ok:
        tail = "\n".join(ev.synth.log.strip().splitlines()[-40:])
        return (
            f"Attempt {attempt} REJECTED: tests pass but synthesis failed.\n\n"
            f"```\n{tail}\n```\n\nMake it elaborate under sv2v and yosys."
        )

    if target.objective == "correctness":
        return (
            f"Attempt {attempt}: {ev.test.summary()} and it synthesizes. "
            "The module is correct. Stop unless you see something clearly wrong."
        )

    now = score(target, ev)
    verdict = (f"IMPROVED (best was {best})" if best is None or now < best
               else f"no improvement (best is still {best})")
    return (
        f"Attempt {attempt} accepted: {ev.test.summary()}, "
        f"{ev.synth.summary()} -- {verdict}.\n\n"
        f"Keep going: reduce {target.objective} further without breaking the "
        "tests or changing the interface."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", choices=sorted(TARGETS))
    ap.add_argument("--mode", choices=("implement", "optimize"), default=None,
                    help="default: implement when the target has a skeleton "
                         "and an objective of correctness, else optimize")
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--model", default=os.environ.get("GEMINI_MODEL",
                                                      "gemini-3.1-pro-preview"))
    ap.add_argument("--location", default=os.environ.get("GEMINI_LOCATION", "global"))
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--branch", default=None)
    args = ap.parse_args()

    target = TARGETS[args.target]
    mode = args.mode or ("implement" if target.skeleton else "optimize")

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("GOOGLE_CLOUD_PROJECT is not set")

    require_clean_tree()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    branch = args.branch or f"agent/{target.name}-{stamp}"
    origin_branch = git("rev-parse", "--abbrev-ref", "HEAD")
    base_commit = git("rev-parse", "--short", "HEAD")
    logs = LOGS_DIR / f"{target.name}-{stamp}"
    logs.mkdir(parents=True, exist_ok=True)

    git("checkout", "-b", branch)
    print(f"{mode} {target.name} on {branch} (off {origin_branch}@{base_commit})")

    rtl_path = REPO / target.rtl
    ev = EvalNode()
    tool = None
    history = []
    verified = []
    best = None
    best_commit = base_commit

    try:
        if mode == "implement":
            # Always start from the skeleton so a run is reproducible and the
            # interface comes from the loop rather than from the agent.
            if not target.skeleton:
                raise SystemExit(f"{target.name} has no skeleton to start from")
            shutil.copy2(REPO / target.skeleton, rtl_path)
            sha = commit(target, f"agent({target.name}): start from skeleton",
                         [f"mode: implement", f"model: {args.model}"])
            if sha:
                print(f"seeded skeleton at {sha}")
            base = evaluate(ev, target, target.params)
            print(f"baseline (stub): {base.describe()}")
        else:
            base = evaluate(ev, target, target.params)
            print(f"baseline: {base.describe()}")
            if not base.usable:
                raise SystemExit("baseline is not usable; nothing to optimize from")
            best = score(target, base)

        history.append({"attempt": 0, "score": best,
                        "tests": base.test.summary(), "commit": base_commit,
                        "cells": base.synth.cells if base.usable else None,
                        "depth": base.synth.depth if base.usable else None})

        llm = VertexGeminiLLM(
            model=args.model, project=project, location=args.location,
            system_message=SYSTEM_MESSAGE, max_tool_iterations=12, retries=2,
            # CHIA defaults to 16000, which truncates here: an attempt emits the
            # whole module as a tool argument on top of the model's reasoning.
            max_tokens=args.max_tokens,
        )
        tool = RtlEditTool(name="rtl_edit", target_file=str(rtl_path),
                           module_name=target.name)

        message = (REPO / target.task).read_text().format(
            n_tests=base.test.tests_run or "the")

        for attempt in range(1, args.attempts + 1):
            print(f"\n=== attempt {attempt}/{args.attempts} ===")
            started = time.time()
            try:
                reply = get(llm.prompt.chia_remote(llm, message, [tool]))
            except Exception as exc:
                # One bad attempt -- truncation, a transient Vertex error, one
                # of this host's OAuth stalls -- costs an iteration, not the run.
                print(f"agent call raised {type(exc).__name__}: {str(exc)[:200]}")
                history.append({"attempt": attempt, "score": None,
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

            cand = evaluate(ev, target, target.params)
            print(f"  {cand.describe()}")

            verify_fail = None
            if cand.usable:
                verified_now, verify_fail = check_verify_points(ev, target)
                for chk in verified_now:
                    print(f"    {chk.describe()}")
                if verify_fail is None and verified_now:
                    verified = [
                        {"params": v.params, "passed": v.test.passed,
                         "cells": v.synth.cells if v.usable else None,
                         "depth": v.synth.depth if v.usable else None}
                        for v in verified_now
                    ]

            complete = cand.usable and verify_fail is None
            now = score(target, cand) if complete else None
            improved = now is not None and (best is None or now < best)
            if improved:
                print("    <- new best")

            if not cand.usable:
                reason = "tests" if not cand.test.passed else "synthesis"
                log = cand.test.log if not cand.test.passed else cand.synth.log
                (logs / f"attempt_{attempt}_{reason}.log").write_text(log)
            elif verify_fail is not None:
                log = (verify_fail.test.log if not verify_fail.test.passed
                       else verify_fail.synth.log)
                (logs / f"attempt_{attempt}_verify.log").write_text(log)

            if cand.usable and verify_fail is not None:
                headline = (f"rejected: fails at {verify_fail.params}")
            elif cand.usable:
                headline = (f"{cand.synth.summary()} "
                            f"({'accepted' if improved else 'no improvement'})")
            elif cand.test.passed:
                headline = "rejected: synthesis failed"
            else:
                headline = f"rejected: {cand.test.summary()}"

            sha = commit(
                target, f"agent({target.name}): attempt {attempt} -- {headline}",
                [f"tests: {cand.test.summary()}",
                 f"point: {cand.params}",
                 f"mode: {mode}",
                 f"model: {args.model}"]
                + ([f"synth: {cand.synth.summary()}"] if cand.synth else []))
            if sha:
                print(f"  committed {sha}")

            history.append({"attempt": attempt, "score": now,
                            "tests": cand.test.summary(), "commit": sha,
                            "cells": cand.synth.cells if cand.usable else None,
                            "depth": cand.synth.depth if cand.usable else None})

            if improved:
                best, best_commit = now, (sha or best_commit)

            # In implement mode the goal is binary: once it passes and
            # synthesizes there is nothing further to ask for.
            if mode == "implement" and complete:
                print("  correct and synthesizing at every parameter point; stopping")
                break

            message = format_feedback(target, attempt, cand, best, verify_fail)
    finally:
        if tool is not None:
            tool.stop()
        # Any straggler edit from a crashed attempt still gets committed, so
        # the checkout below cannot fail on a dirty tree and nothing is lost.
        if git("status", "--porcelain", "--", target.rtl):
            git("add", "--", target.rtl)
            git("commit", "-m", f"agent({target.name}): uncommitted edit at exit")

    # Leave the branch tip at the best design rather than at whatever the last
    # attempt produced, so `git diff <origin>..<branch>` is the result.
    if best_commit != git("rev-parse", "--short", "HEAD"):
        git("checkout", best_commit, "--", target.rtl)
        if git("status", "--porcelain"):
            git("commit", "-m",
                f"agent({target.name}): restore best design from {best_commit}")
            print(f"\nrestored best design from {best_commit}")

    print("\n=== result ===")
    if best is None:
        print(f"{target.name}: no usable design produced")
    elif target.objective == "correctness":
        print(f"{target.name}: correct and synthesizing")
    else:
        print(f"{target.name}: {target.objective} "
              f"{history[0]['score']} -> {best}")

    if verified:
        print("\nverified at:")
        for v in verified:
            print(f"  {v['params']}: "
                  + ("ok" if v["passed"] else "FAILED")
                  + (f", cells={v['cells']} depth={v['depth']}"
                     if v["cells"] is not None else ""))

    (logs / "summary.json").write_text(json.dumps({
        "target": target.name, "mode": mode, "model": args.model,
        "branch": branch, "base": base_commit, "objective": target.objective,
        "history": history, "best": best, "verified": verified,
    }, indent=2))

    git("checkout", origin_branch)
    print(f"\nback on {origin_branch}; the run is on {branch}")
    print(f"  git diff {origin_branch}..{branch}")
    print(f"  git merge {branch}      # keep it")
    print(f"  git branch -D {branch}  # or don't")


if __name__ == "__main__":
    main()
