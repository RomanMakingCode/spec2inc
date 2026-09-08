"""Trivial cocotb testbench for counter.sv - proves the CHIA + Docker +
Verilator + cocotb hookup works end-to-end before any real fabric RTL
is written.
"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge


@cocotb.test()
async def counter_counts_up(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())

    dut.rst_n.value = 0
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    # Deassert away from the clock edge (on FallingEdge, not immediately
    # before the next RisingEdge) so rst_n is stable well before the DUT
    # samples it - otherwise the write can race the very edge that's
    # supposed to see it deasserted.
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)

    prev = int(dut.count.value)
    for _ in range(20):
        await RisingEdge(dut.clk)
        cur = int(dut.count.value)
        expected = (prev + 1) & 0xF
        assert cur == expected, f"counter mismatch: expected {expected}, got {cur}"
        prev = cur
