"""Smoke test proving the cocotb+Verilator hookup works end-to-end on a
single-machine CHIA cluster.

    export THIS_MACHINE=$(hostname -I | awk '{print $1}')
    chia up examples/smoke_test/cluster.yaml
    python examples/smoke_test/loop.py
    chia down examples/smoke_test/cluster.yaml
"""
from pathlib import Path

from chia.base.ChiaFunction import get
from cocotb_run_node import CocotbRunNode


def main():
    node = CocotbRunNode()
    test_dir = str(Path(__file__).parent.resolve())

    result = get(node.run.chia_remote(node, test_dir))

    print(f"passed={result.passed} tests_run={result.tests_run} tests_failed={result.tests_failed}")
    print(result.log)
    assert result.passed, "smoke test failed"
    print("SMOKE TEST OK: cocotb + Verilator + CHIA hookup works.")


if __name__ == "__main__":
    main()
