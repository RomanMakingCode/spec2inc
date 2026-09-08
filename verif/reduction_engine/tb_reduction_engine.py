"""Unit testbench for reduction_engine. Contract: docs/modules.md 3.1.

Each test maps to one bullet of that contract. Expected values come from
verif/reference.py -- never from a second implementation written here, since a
checker that reimplements the DUT tends to reproduce its bugs.

Timing convention: every drive and every sample happens just after a falling
edge. The DUT is synchronous to the rising edge, so mid-cycle values are settled
and neither driving nor sampling can race the edge that matters.
"""

import os
import random
import sys
from pathlib import Path

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reference import LANE_DTYPE, reduce_lanes  # noqa: E402

# Mirrors spec2inc_pkg. Kept here rather than parsed out of the package because
# the testbench must fail loudly if the contract shifts underneath it.
DATA_W = 256
LANE_W = 32
LANES = DATA_W // LANE_W

N_PORTS = int(os.environ.get("N_PORTS", "16"))
RED_LATENCY = int(os.environ.get("RED_LATENCY", "3"))

LANE_MAX = (1 << LANE_W) - 1


def beat_from_lanes(lanes) -> int:
    """Pack LANES lane values into one DATA_W-bit beat, lane 0 in the low bits."""
    beat = 0
    for i, lane in enumerate(lanes):
        beat |= (int(lane) & LANE_MAX) << (i * LANE_W)
    return beat


def lanes_from_beat(beat: int) -> list[int]:
    return [(beat >> (i * LANE_W)) & LANE_MAX for i in range(LANES)]


def pack_operands(operands: dict[int, list[int]]) -> int:
    """Pack {slot: lane list} into the flat op_data vector."""
    packed = 0
    for slot, lanes in operands.items():
        packed |= beat_from_lanes(lanes) << (slot * DATA_W)
    return packed


def expected_beat(operands: dict[int, list[int]], mask: int) -> list[int]:
    """Reduction of the masked operands, via the frozen reference model."""
    contributing = [
        np.array(lanes, dtype=LANE_DTYPE)
        for slot, lanes in sorted(operands.items())
        if mask >> slot & 1
    ]
    return [int(x) for x in reduce_lanes(contributing)]


async def reset(dut):
    dut.op_valid.value = 0
    dut.op_mask.value = 0
    dut.op_data.value = 0
    dut.op_id.value = 0
    dut.res_ready.value = 1
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)


async def send_op(dut, mask: int, operands: dict[int, list[int]], op_id: int):
    """Present one operand set. Leaves the sim at the negedge after acceptance.

    Assumes res_ready is high, so op_ready is high and the operand set is taken
    at the next rising edge. The backpressure test drives the handshake itself.
    """
    assert dut.op_ready.value == 1, "op_ready low with res_ready high"
    dut.op_mask.value = mask
    dut.op_data.value = pack_operands(operands)
    dut.op_id.value = op_id
    dut.op_valid.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.op_valid.value = 0


async def await_result(dut, limit: int = 64):
    """Cycles elapsed since acceptance, plus the result. Call after send_op."""
    elapsed = 1  # the acceptance edge itself
    while elapsed <= limit:
        if dut.res_valid.value == 1:
            return elapsed, int(dut.res_data.value), int(dut.res_id.value)
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        elapsed += 1
    raise AssertionError(f"no result within {limit} cycles")


def random_operands(rng, slots) -> dict[int, list[int]]:
    return {s: [rng.randrange(0, 1 << LANE_W) for _ in range(LANES)] for s in slots}


@cocotb.test()
async def reduces_to_reference(dut):
    """res_data lane i is the sum of lane i over the masked operands."""
    await start(dut)
    rng = random.Random(0xC0FFEE)

    for trial in range(24):
        n_set = rng.randint(2, N_PORTS)
        slots = rng.sample(range(N_PORTS), n_set)
        mask = sum(1 << s for s in slots)
        operands = random_operands(rng, range(N_PORTS))  # unmasked slots too
        op_id = rng.randrange(0, 256)

        await send_op(dut, mask, operands, op_id)
        _, data, _ = await await_result(dut)

        got = lanes_from_beat(data)
        want = expected_beat(operands, mask)
        assert got == want, (
            f"trial {trial}: mask={mask:#x} n_set={n_set}\n  got  {got}\n  want {want}"
        )


@cocotb.test()
async def unmasked_operands_are_ignored(dut):
    """Data in slots outside the mask must not reach the result.

    Distinct from the randomized test: here every unmasked slot is loaded with a
    value large enough that including even one of them would be unmistakable.
    """
    await start(dut)
    mask_slots = [0, 1]
    mask = sum(1 << s for s in mask_slots)

    operands = {s: [1] * LANES for s in mask_slots}
    for s in range(N_PORTS):
        if s not in mask_slots:
            operands[s] = [0xDEADBEEF] * LANES

    await send_op(dut, mask, operands, 0x11)
    _, data, _ = await await_result(dut)

    assert lanes_from_beat(data) == [2] * LANES, (
        f"unmasked operands leaked into the result: {lanes_from_beat(data)}"
    )


@cocotb.test()
async def latency_is_exactly_red_latency(dut):
    """Result appears exactly RED_LATENCY cycles after acceptance."""
    await start(dut)
    rng = random.Random(7)

    for _ in range(8):
        operands = random_operands(rng, range(N_PORTS))
        mask = 0b11
        await send_op(dut, mask, operands, 0x22)
        elapsed, _, _ = await await_result(dut)
        assert elapsed == RED_LATENCY, (
            f"latency {elapsed}, expected RED_LATENCY={RED_LATENCY}"
        )


@cocotb.test()
async def id_is_carried_through(dut):
    """res_id matches the op_id of the transaction that produced the result."""
    await start(dut)
    rng = random.Random(11)

    for op_id in (0x00, 0x01, 0x5A, 0xA5, 0xFF):
        operands = random_operands(rng, range(N_PORTS))
        await send_op(dut, 0b11, operands, op_id)
        _, _, got_id = await await_result(dut)
        assert got_id == op_id, f"res_id {got_id:#x}, expected {op_id:#x}"


@cocotb.test()
async def single_member_is_passthrough(dut):
    """A mask with one bit set returns that operand unchanged."""
    await start(dut)
    rng = random.Random(13)

    for slot in (0, 1, N_PORTS // 2, N_PORTS - 1):
        operands = random_operands(rng, range(N_PORTS))
        await send_op(dut, 1 << slot, operands, slot & 0xFF)
        _, data, _ = await await_result(dut)
        assert lanes_from_beat(data) == operands[slot], (
            f"slot {slot} not passed through unchanged"
        )


@cocotb.test()
async def lanes_wrap_independently(dut):
    """Lanes wrap at 2**32 and never carry into the neighbouring lane."""
    await start(dut)

    # Every lane of slot 0 is max; slot 1 adds one to alternating lanes. Wrapped
    # lanes must read 0 and untouched lanes must read max -- a carry across the
    # lane boundary would corrupt the neighbour.
    a = [LANE_MAX] * LANES
    b = [1 if i % 2 == 0 else 0 for i in range(LANES)]
    operands = {s: [0] * LANES for s in range(N_PORTS)}
    operands[0] = a
    operands[1] = b

    await send_op(dut, 0b11, operands, 0x33)
    _, data, _ = await await_result(dut)

    want = [0 if i % 2 == 0 else LANE_MAX for i in range(LANES)]
    assert lanes_from_beat(data) == want, (
        f"lane wrap wrong:\n  got  {lanes_from_beat(data)}\n  want {want}"
    )


@cocotb.test()
async def result_is_independent_of_slot_placement(dut):
    """The same multiset of operands reduces identically wherever it sits.

    This is the order-independence property the block engine relies on to
    collect member reads in arbitrary order.
    """
    await start(dut)
    rng = random.Random(17)
    values = [[rng.randrange(0, 1 << LANE_W) for _ in range(LANES)] for _ in range(4)]

    results = []
    for placement in ([0, 1, 2, 3], [N_PORTS - 1, 0, N_PORTS // 2, 1]):
        operands = {s: [0] * LANES for s in range(N_PORTS)}
        for value, slot in zip(values, placement):
            operands[slot] = value
        mask = sum(1 << s for s in placement)

        await send_op(dut, mask, operands, 0x44)
        _, data, _ = await await_result(dut)
        results.append(lanes_from_beat(data))

    assert results[0] == results[1], (
        f"slot placement changed the result:\n  {results[0]}\n  {results[1]}"
    )
    assert results[0] == expected_beat(
        {i: v for i, v in enumerate(values)}, 0b1111
    ), "placement-independent result still disagrees with the reference"


@cocotb.test()
async def backpressure_holds_result_and_stalls_input(dut):
    """While res_ready is low the result is held stable and no operand is lost."""
    await start(dut)
    rng = random.Random(19)

    operands = random_operands(rng, range(N_PORTS))
    mask = 0b111
    want = expected_beat(operands, mask)

    dut.res_ready.value = 0
    await send_op(dut, mask, operands, 0x55)

    # Advance until the result reaches the output stage, then hold it there.
    for _ in range(RED_LATENCY + 2):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)

    assert dut.res_valid.value == 1, "result never became valid under backpressure"
    assert dut.op_ready.value == 0, "op_ready stayed high while output was stalled"

    for _ in range(5):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        assert dut.res_valid.value == 1, "res_valid dropped before acceptance"
        assert lanes_from_beat(int(dut.res_data.value)) == want, (
            "res_data changed while stalled"
        )
        assert int(dut.res_id.value) == 0x55, "res_id changed while stalled"

    # Release, and confirm the engine accepts work again.
    dut.res_ready.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    assert dut.op_ready.value == 1, "op_ready did not recover after backpressure"

    follow = random_operands(rng, range(N_PORTS))
    await send_op(dut, 0b11, follow, 0x66)
    _, data, got_id = await await_result(dut)
    assert got_id == 0x66, "transaction after backpressure was lost or reordered"
    assert lanes_from_beat(data) == expected_beat(follow, 0b11)


@cocotb.test()
async def back_to_back_operands_stream(dut):
    """One operand set per cycle, with results emerging in order.

    The results start draining while later operands are still being driven, so
    collection has to run concurrently with driving -- sampling only after the
    send loop would miss every result the pipeline had already handed back.
    """
    await start(dut)
    rng = random.Random(23)

    seen = []
    monitoring = True

    async def monitor():
        while monitoring:
            await FallingEdge(dut.clk)
            if dut.res_valid.value == 1 and dut.res_ready.value == 1:
                seen.append(
                    (int(dut.res_id.value), lanes_from_beat(int(dut.res_data.value)))
                )

    cocotb.start_soon(monitor())

    sent = []
    for i in range(8):
        operands = random_operands(rng, range(N_PORTS))
        mask = 0b11 if i % 2 else 0b111
        sent.append((i & 0xFF, expected_beat(operands, mask)))

        dut.op_mask.value = mask
        dut.op_data.value = pack_operands(operands)
        dut.op_id.value = i & 0xFF
        dut.op_valid.value = 1
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)

    dut.op_valid.value = 0

    for _ in range(RED_LATENCY + 4):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    monitoring = False

    assert seen == sent, (
        "streamed results wrong or out of order\n"
        f"  got ids  {[i for i, _ in seen]}\n"
        f"  want ids {[i for i, _ in sent]}"
    )
