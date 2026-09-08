"""Evaluation nodes for the optimization loop.

These belong to the loop, not to the agent. The agent never invokes them and
cannot influence what they report -- see tools.py for why that separation
matters.

Both run on a worker advertising cocotb_run, which is the containerized image
carrying verilator, cocotb, sv2v, and yosys.
"""

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

from chia.base.ChiaFunction import ChiaFunction

# cocotb's own summary line, e.g. "** TESTS=9 PASS=9 FAIL=0 SKIP=0 ... **".
# Parsed with a regex rather than by counting PASS/FAIL substrings in the log:
# those words also appear in the table header and in per-test rows, so counting
# them reports failures that did not happen.
_SUMMARY_RE = re.compile(r"TESTS=(\d+)\s+PASS=(\d+)\s+FAIL=(\d+)\s+SKIP=(\d+)")

_CELLS_RE = re.compile(r"^\s+(\d+)\s+cells\s*$", re.MULTILINE)
_LTP_RE = re.compile(r"Longest topological path in \w+ \(length=(\d+)\)")


@dataclass
class TestResult:
    passed: bool
    tests_run: int
    tests_failed: int
    log: str

    def summary(self) -> str:
        if self.passed:
            return f"all {self.tests_run} tests passed"
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

    n_ports: int
    red_latency: int
    test: TestResult
    synth: SynthResult = field(default=None)

    @property
    def usable(self) -> bool:
        return self.test.passed and self.synth is not None and self.synth.ok


class EvalNode:
    """Runs the unit testbench and the synthesis gate on a candidate design."""

    logging_name = "EvalNode"

    def __init__(self, logging_level: int = logging.INFO):
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @ChiaFunction(resources={"cocotb_run": 1})
    def run_tests(self, workdir: str, n_ports: int, red_latency: int) -> TestResult:
        """Run the frozen cocotb testbench against whatever RTL is in workdir."""
        test_dir = os.path.join(workdir, "verif", "reduction_engine")
        env = {
            **os.environ,
            "N_PORTS": str(n_ports),
            "RED_LATENCY": str(red_latency),
        }
        subprocess.run(["make", "clean"], cwd=test_dir, env=env,
                       capture_output=True, text=True)
        proc = subprocess.run(
            ["make", f"N_PORTS={n_ports}", f"RED_LATENCY={red_latency}"],
            cwd=test_dir, env=env, capture_output=True, text=True,
        )
        log = proc.stdout + "\n" + proc.stderr

        match = _SUMMARY_RE.search(log)
        if not match:
            # No summary line at all means the build or elaboration died before
            # any test ran, which is a failure the agent needs to see verbatim.
            return TestResult(passed=False, tests_run=0, tests_failed=0, log=log)

        total, passed, failed, _ = (int(g) for g in match.groups())
        return TestResult(
            passed=(failed == 0 and passed == total and total > 0),
            tests_run=total,
            tests_failed=failed,
            log=log,
        )

    @ChiaFunction(resources={"cocotb_run": 1})
    def run_synth(self, workdir: str, n_ports: int) -> SynthResult:
        """Convert with sv2v and synthesize, reporting cell count and logic depth.

        Depth is yosys's longest topological path: a PDK-free stand-in for
        critical path, which is what makes this measurable without a standard
        cell library.
        """
        rtl = os.path.join(workdir, "rtl")
        flat = os.path.join(workdir, "flat.v")

        conv = subprocess.run(
            f"sv2v {rtl}/spec2inc_pkg.sv {rtl}/reduction_engine.sv > {flat}",
            shell=True, capture_output=True, text=True,
        )
        if conv.returncode != 0:
            return SynthResult(ok=False, cells=0, depth=0,
                               log="sv2v failed:\n" + conv.stderr)

        script = (
            f"read_verilog {flat}; "
            f"chparam -set N_PORTS {n_ports} reduction_engine; "
            f"synth -top reduction_engine; stat; ltp"
        )
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
