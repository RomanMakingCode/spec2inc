"""Unit testbench for group_table. Contract: docs/modules.md 3.2.

Each test maps to one bullet of that contract. The oracle is a Python dict --
this module stores things and hands them back, so there is nothing to model
beyond that, which is what makes it the cheapest place to prove an
implement-from-spec loop works.

Timing convention: writes are driven just after a falling edge, so they are
stable well before the rising edge that commits them. Combinational reads are
checked mid-cycle after a settling delay, never at an edge.
"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

# Mirrors spec2inc_pkg. Hardcoded rather than parsed so the testbench fails
# loudly if the frozen contract shifts underneath it.
MAX_PORTS = 64
ID_W = 6
ID_SPACE = 1 << ID_W

N_PORTS = int(os.environ.get("N_PORTS", "16"))
GROUP_TABLE_ENTRIES = int(os.environ.get("GROUP_TABLE_ENTRIES", "64"))

MASK_ALL = (1 << MAX_PORTS) - 1

# Long enough for combinational logic to settle, short enough to stay inside
# the half-cycle. The clock is 10ns.
SETTLE_NS = 1


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.wr_en.value = 0
    dut.wr_idx.value = 0
    dut.wr_valid_bit.value = 0
    dut.wr_members.value = 0
    dut.rd_idx.value = 0
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)


def as_int(signal):
    """Signal value, or None if it contains X/Z.

    A candidate implementation may leave outputs undriven, and int() on an X
    raises rather than failing the assertion that was actually being made. This
    turns that into a value the tests can report on.
    """
    try:
        return int(signal.value)
    except Exception:
        return None


async def read(dut, idx: int):
    """Combinational read: drive the index, settle, sample. No clock edge."""
    dut.rd_idx.value = idx
    await Timer(SETTLE_NS, unit="ns")
    return as_int(dut.rd_valid_bit), as_int(dut.rd_members)


async def write(dut, idx: int, valid: bool, members: int):
    """Synchronous write, committed on the next rising edge."""
    dut.wr_en.value = 1
    dut.wr_idx.value = idx
    dut.wr_valid_bit.value = 1 if valid else 0
    dut.wr_members.value = members
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.wr_en.value = 0


@cocotb.test()
async def reset_clears_every_entry(dut):
    """After reset no entry reads valid, whatever was there before."""
    await start(dut)

    # Fill the table, then reset, so a stuck-valid implementation is caught
    # rather than passing because the array happened to start at zero.
    for idx in range(GROUP_TABLE_ENTRIES):
        await write(dut, idx, True, MASK_ALL)

    # Confirm the fill actually took before resetting. Without this the test
    # is satisfied by an implementation that never sets a valid bit at all --
    # "nothing is valid after reset" being vacuously true.
    for idx in (0, GROUP_TABLE_ENTRIES - 1):
        valid, _ = await read(dut, idx)
        assert valid == 1, (
            f"entry {idx} never became valid, so this test cannot say "
            "anything about reset"
        )

    dut.rst_n.value = 0
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)

    for idx in range(GROUP_TABLE_ENTRIES):
        valid, _ = await read(dut, idx)
        assert valid == 0, f"entry {idx} still valid after reset"


@cocotb.test()
async def write_then_read_returns_the_entry(dut):
    """A written entry reads back exactly, valid bit and mask."""
    await start(dut)
    rng = random.Random(0xA11CE)

    for idx in (0, 1, GROUP_TABLE_ENTRIES // 2, GROUP_TABLE_ENTRIES - 1):
        members = rng.getrandbits(MAX_PORTS)
        await write(dut, idx, True, members)
        valid, got = await read(dut, idx)
        assert valid == 1, f"entry {idx} not valid after write"
        assert got == members, (
            f"entry {idx} mask wrong:\n  got  {got:#018x}\n  want {members:#018x}"
        )


@cocotb.test()
async def reads_are_combinational(dut):
    """Changing rd_idx changes the outputs within the same cycle.

    A registered read would still be showing the previous index here, which
    would break port_ingress: it resolves a group in the cycle it classifies.
    """
    await start(dut)

    a_members = 0x0000_0000_0000_00FF
    b_members = 0xFF00_0000_0000_0000
    await write(dut, 3, True, a_members)
    await write(dut, 7, True, b_members)

    # Both reads happen inside one clock cycle, with no edge between them.
    dut.rd_idx.value = 3
    await Timer(SETTLE_NS, unit="ns")
    assert as_int(dut.rd_members) == a_members, "first combinational read wrong"

    dut.rd_idx.value = 7
    await Timer(SETTLE_NS, unit="ns")
    assert as_int(dut.rd_members) == b_members, (
        "second read in the same cycle did not follow rd_idx -- read appears registered"
    )


@cocotb.test()
async def writes_are_synchronous(dut):
    """A write is not visible until the edge that commits it."""
    await start(dut)

    old = 0x0000_0000_0000_0F0F
    new = 0x0000_0000_0000_F0F0
    await write(dut, 5, True, old)

    # Present the new value and read the same index before the clock edge.
    dut.wr_en.value = 1
    dut.wr_idx.value = 5
    dut.wr_valid_bit.value = 1
    dut.wr_members.value = new
    dut.rd_idx.value = 5
    await Timer(SETTLE_NS, unit="ns")
    assert as_int(dut.rd_members) == old, (
        "write became visible before the clock edge -- storage is not registered"
    )

    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.wr_en.value = 0

    _, got = await read(dut, 5)
    assert got == new, "write did not take effect on the clock edge"


@cocotb.test()
async def entries_are_independent(dut):
    """Writing one entry disturbs no other."""
    await start(dut)
    rng = random.Random(0xBEEF)

    model = {}
    for idx in range(GROUP_TABLE_ENTRIES):
        model[idx] = rng.getrandbits(MAX_PORTS)
        await write(dut, idx, True, model[idx])

    for idx, want in model.items():
        valid, got = await read(dut, idx)
        assert valid == 1 and got == want, (
            f"entry {idx} disturbed: got {got:#018x}, want {want:#018x}"
        )

    # Overwrite a few and re-check the whole table, catching an implementation
    # that decodes the write index too loosely.
    for idx in rng.sample(range(GROUP_TABLE_ENTRIES),
                          min(4, GROUP_TABLE_ENTRIES)):
        model[idx] = rng.getrandbits(MAX_PORTS)
        await write(dut, idx, True, model[idx])

    for idx, want in model.items():
        valid, got = await read(dut, idx)
        assert valid == 1 and got == want, (
            f"entry {idx} disturbed by an unrelated write: "
            f"got {got:#018x}, want {want:#018x}"
        )


@cocotb.test()
async def entries_can_be_invalidated(dut):
    """Writing with the valid bit clear makes the entry read invalid."""
    await start(dut)

    await write(dut, 2, True, MASK_ALL)
    valid, _ = await read(dut, 2)
    assert valid == 1, "setup write did not take"

    await write(dut, 2, False, MASK_ALL)
    valid, _ = await read(dut, 2)
    assert valid == 0, "entry still valid after being written with valid=0"


@cocotb.test()
async def out_of_range_reads_are_invalid(dut):
    """Indices beyond the table read invalid rather than aliasing into it.

    Skipped when the table covers the whole index space, since then there is no
    out-of-range index to test.
    """
    if GROUP_TABLE_ENTRIES >= ID_SPACE:
        cocotb.log.info(
            f"GROUP_TABLE_ENTRIES={GROUP_TABLE_ENTRIES} covers the {ID_SPACE}-entry "
            "index space; nothing out of range to check"
        )
        return

    await start(dut)

    for idx in range(GROUP_TABLE_ENTRIES):
        await write(dut, idx, True, MASK_ALL)

    for idx in range(GROUP_TABLE_ENTRIES, ID_SPACE):
        valid, members = await read(dut, idx)
        assert valid == 0, (
            f"out-of-range index {idx} read valid -- it is aliasing onto "
            f"entry {idx % GROUP_TABLE_ENTRIES}"
        )
        assert members == 0, f"out-of-range index {idx} returned mask {members:#018x}"
