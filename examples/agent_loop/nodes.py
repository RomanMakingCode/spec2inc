"""Evaluation nodes for the agent loop.

These belong to the loop, not to the agent. The agent never invokes them and
cannot influence what they report -- see tools.py for why that separation
matters.

Both run on a worker advertising cocotb_run, the containerized image carrying
verilator, cocotb, sv2v, and yosys.
"""

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from chia.base.ChiaFunction import ChiaFunction

# cocotb's own summary line, e.g. "** TESTS=9 PASS=9 FAIL=0 SKIP=0 ... **".
# Parsed with a regex rather than by counting PASS/FAIL substrings in the log:
# those words also appear in the table header and in per-test rows, so counting
# them reports failures that did not happen.
_SUMMARY_RE = re.compile(r"TESTS=(\d+)\s+PASS=(\d+)\s+FAIL=(\d+)\s+SKIP=(\d+)")

_CELLS_RE = re.compile(r"^\s+(\d+)\s+cells\s*$", re.MULTILINE)
_LTP_RE = re.compile(r"Longest topological path in \w+ \(length=(\d+)\)")


def _params_str(params: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(params.items()))


@dataclass
class TestResult:
    passed: bool
    tests_run: int
    tests_failed: int
    log: str

    def summary(self) -> str:
        if self.passed:
            return f"all {self.tests_run} tests passed"
        if self.tests_run == 0:
            # No summary line means elaboration or the build died before any
            # test ran. Saying "0 of 0 tests FAILED" reads as nonsense, and this
            # string is fed back to the agent as its diagnosis.
            return "DID NOT COMPILE: no tests ran"
        return f"{self.tests_failed} of {self.tests_run} tests FAILED"


@dataclass
class SynthResult:
    ok: bool
    cells: int
    depth: int          # longest topological path, in logic levels
    log: str

    def summary(self) -> str:
        if not self.ok:
            return "synthesis FAILED"
        return f"cells={self.cells} logic_depth={self.depth}"


@dataclass
class Evaluation:
    """One full measurement of a candidate design, at one parameter point."""

    params: dict
    test: TestResult
    synth: Optional[SynthResult] = field(default=None)

    @property
    def usable(self) -> bool:
        return self.test.passed and self.synth is not None and self.synth.ok

    def describe(self) -> str:
        at = _params_str(self.params)
        if self.usable:
            return f"{at}: {self.test.summary()}, {self.synth.summary()}"
        return f"{at}: {self.test.summary()}"


class EvalNode:
    """Runs a module's unit testbench and the synthesis gate on a candidate."""

    logging_name = "EvalNode"

    def __init__(self, logging_level: int = logging.INFO):
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @ChiaFunction(resources={"cocotb_run": 1})
    def run_tests(self, workdir: str, tb_dir: str, params: dict) -> TestResult:
        """Run a module's frozen testbench against whatever RTL is in workdir.

        Parameters reach the RTL as make variables (which the Makefile forwards
        to verilator as -G) and the testbench as environment variables, from the
        same dict, so the two cannot disagree about what was built.
        """
        test_dir = os.path.join(workdir, tb_dir)
        env = {**os.environ, **{k: str(v) for k, v in params.items()}}
        make_args = [f"{k}={v}" for k, v in sorted(params.items())]

        subprocess.run(["make", "clean"], cwd=test_dir, env=env,
                       capture_output=True, text=True)
        proc = subprocess.run(["make", *make_args], cwd=test_dir, env=env,
                              capture_output=True, text=True)
        log = proc.stdout + "\n" + proc.stderr

        match = _SUMMARY_RE.search(log)
        if not match:
            return TestResult(passed=False, tests_run=0, tests_failed=0, log=log)

        total, passed, failed, _ = (int(g) for g in match.groups())
        return TestResult(
            passed=(failed == 0 and passed == total and total > 0),
            tests_run=total,
            tests_failed=failed,
            log=log,
        )

    @ChiaFunction(resources={"cocotb_run": 1})
    def run_synth(self, workdir: str, sources: list, top: str,
                  params: dict) -> SynthResult:
        """Convert with sv2v and synthesize, reporting cell count and depth.

        Depth is yosys's longest topological path: a PDK-free stand-in for
        critical path, which is what makes it measurable without a standard
        cell library.
        """
        srcs = " ".join(os.path.join(workdir, s) for s in sources)

        # Intermediate Verilog goes to a temp dir, never into workdir: the loop
        # runs against the repo itself, and a stray artifact there would dirty
        # the tree and block the next run's clean-tree check.
        with tempfile.TemporaryDirectory(prefix="spec2inc_synth_") as tmp:
            flat = os.path.join(tmp, "flat.v")
            conv = subprocess.run(f"sv2v {srcs} > {flat}", shell=True,
                                  capture_output=True, text=True)
            if conv.returncode != 0:
                return SynthResult(ok=False, cells=0, depth=0,
                                   log="sv2v failed:\n" + conv.stderr)

            chparam = "".join(f"chparam -set {k} {v} {top}; "
                              for k, v in sorted(params.items()))
            script = (f"read_verilog {flat}; {chparam}"
                      f"synth -top {top}; stat; ltp")
            proc = subprocess.run(["yosys", "-p", script],
                                  capture_output=True, text=True)
            log = proc.stdout + "\n" + proc.stderr

        if proc.returncode != 0:
            return SynthResult(ok=False, cells=0, depth=0, log=log)

        cells = _CELLS_RE.findall(log)
        depth = _LTP_RE.search(log)
        if not cells or not depth:
            return SynthResult(ok=False, cells=0, depth=0, log=log)

        return SynthResult(ok=True, cells=int(cells[-1]),
                           depth=int(depth.group(1)), log=log)
