"""Custom CHIA node for running a cocotb testbench against Verilator.

Follows the same authoring pattern as chia.chipyard.verilator_run_node
(a plain class holding config/state, with one @ChiaFunction-decorated
method as the dispatchable node) but targets standalone Verilog/
SystemVerilog RTL driven by cocotb's Makefile-based runner instead of a
full Chipyard SoC build. See docs/user_guides/chia_function.rst for the
general pattern this follows.
"""
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from chia.base.ChiaFunction import ChiaFunction


@dataclass
class CocotbResult:
    passed: bool
    tests_run: int
    tests_failed: int
    log: str
    return_code: int


class CocotbRunNode:
    """Runs a cocotb testbench (via its Makefile) against Verilator."""

    logging_name = "CocotbRunNode"

    def __init__(self, logging_level: int = logging.INFO):
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @ChiaFunction(resources={"cocotb_run": 1})
    def run(self, test_dir: str, extra_env: Optional[dict] = None) -> CocotbResult:
        """Run `make` in test_dir, a cocotb test directory whose Makefile
        sets SIM=verilator, TOPLEVEL, MODULE, and VERILOG_SOURCES.
        """
        env = {**os.environ, **(extra_env or {})}
        proc = subprocess.run(
            ["make"],
            cwd=test_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        log = proc.stdout + "\n" + proc.stderr
        # Quick heuristic on cocotb's stdout summary line. Swap for real
        # results.xml (JUnit) parsing once the test suite grows past a
        # single smoke test.
        passed = proc.returncode == 0 and "FAIL=0" in log.replace(" ", "")
        return CocotbResult(
            passed=passed,
            tests_run=log.count("PASS") + log.count("FAIL"),
            tests_failed=log.count("FAIL="),
            log=log,
            return_code=proc.returncode,
        )
