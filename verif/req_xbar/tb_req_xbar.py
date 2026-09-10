"""Unit testbench for req_xbar. Contract: docs/modules.md 3.5.

The oracle is conservation, not computation: this module moves beats without
changing them, so what has to be checked is that every beat injected at a
source arrives once at the sink its dst names, in the order its flow sent it,
without being duplicated, dropped, misrouted, or interleaved with another
packet at the same sink.

Structure: one driver coroutine per source, one monitor coroutine per sink, and
a scoreboard comparing what went in against what came out. Every beat carries a
unique marker in its data field so a duplicate or a swap is unambiguous rather
than inferred.

Timing convention: drivers present a beat and hold it until the cycle where
valid and ready are both high; monitors sample at the same instant. Both look at
the settled mid-cycle value, never at an edge.
"""

import os
import random
from collections import defaultdict

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

N_PORTS = int(os.environ.get("N_PORTS", "8"))
N_SRC = N_PORTS + 2
PRIM_SRC = N_PORTS          # the primitive engine's source index
BLOCK_SRC = N_PORTS + 1     # the block engine's source index

# Field widths, mirroring spec2inc_pkg. Hardcoded rather than parsed so the
# testbench fails loudly if the frozen contract shifts underneath it.
OP_W, ID_W, TAG_W, ADDR_W, LEN_W, DATA_W = 3, 6, 8, 48, 16, 256
REQ_W = OP_W + 2 * ID_W + TAG_W + ADDR_W + LEN_W + DATA_W + 1   # 344

REQ_READ, REQ_WRITE = 0, 1

_FIELDS = (("op", OP_W), ("src", ID_W), ("dst", ID_W), ("tag", TAG_W),
           ("addr", ADDR_W), ("len", LEN_W), ("data", DATA_W), ("last", 1))


def pack(**kw) -> int:
    """Pack a req_t. First field declared is the most significant."""
    v = 0
    for name, width in _FIELDS:
        v = (v << width) | (int(kw.get(name, 0)) & ((1 << width) - 1))
    return v


def unpack(v: int) -> dict:
    out = {}
    for name, width in reversed(_FIELDS):
        out[name] = v & ((1 << width) - 1)
        v >>= width
    return out


def as_int(signal):
    """Signal value, or None if it contains X/Z."""
    try:
        return int(signal.value)
    except Exception:
        return None


class Beat:
    """One injected beat, and where it is expected to land."""

    __slots__ = ("src", "dst", "tag", "marker", "last", "seq")

    def __init__(self, src, dst, tag, marker, last, seq):
        self.src, self.dst, self.tag = src, dst, tag
        self.marker, self.last, self.seq = marker, last, seq

    def word(self) -> int:
        return pack(op=REQ_WRITE, src=self.src, dst=self.dst, tag=self.tag,
                    addr=self.seq, len=1, data=self.marker, last=self.last)

    def __repr__(self):
        return (f"Beat(src={self.src} dst={self.dst} tag={self.tag} "
                f"marker={self.marker:#x} last={self.last})")


def make_packets(rng, n_packets, srcs, dsts, max_beats=3):
    """Build per-source beat queues. Marker values are globally unique."""
    queues = defaultdict(list)
    marker = 1
    for _ in range(n_packets):
        src = rng.choice(srcs)
        dst = rng.choice(dsts)
        tag = rng.randrange(1 << TAG_W)
        n_beats = rng.randint(1, max_beats)
        for i in range(n_beats):
            queues[src].append(
                Beat(src, dst, tag, marker, int(i == n_beats - 1), i))
            marker += 1
    return queues


async def reset(dut):
    dut.s_valid.value = 0
    dut.s_req.value = 0
    dut.m_ready.value = (1 << N_PORTS) - 1
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


class Harness:
    """Drives every source, monitors every sink, and records what happened."""

    def __init__(self, dut, rng):
        self.dut = dut
        self.rng = rng
        self.seen = defaultdict(list)     # sink -> [unpacked beat]
        self.sent = defaultdict(list)     # source -> [Beat]
        self.accepted = defaultdict(int)  # source -> beats accepted
        self.running = True
        self._src_words = [0] * N_SRC
        self._src_valid = [0] * N_SRC

    def _drive_bus(self):
        packed = 0
        for s in range(N_SRC):
            packed |= self._src_words[s] << (s * REQ_W)
        self.dut.s_req.value = packed
        self.dut.s_valid.value = sum(v << s for s, v in enumerate(self._src_valid))

    async def source(self, idx, beats, gap=0.0):
        """Present each beat until it is accepted, then move to the next."""
        for beat in beats:
            # Optional idle gaps, so arbitration is exercised with sources that
            # are not all permanently ready.
            while gap and self.rng.random() < gap:
                await RisingEdge(self.dut.clk)
                await FallingEdge(self.dut.clk)

            self._src_words[idx] = beat.word()
            self._src_valid[idx] = 1
            self._drive_bus()
            while True:
                await RisingEdge(self.dut.clk)
                await FallingEdge(self.dut.clk)
                ready = as_int(self.dut.s_ready)
                if ready is not None and (ready >> idx) & 1:
                    break
                self._drive_bus()
            self.sent[idx].append(beat)
            self.accepted[idx] += 1
            self._src_valid[idx] = 0
            self._drive_bus()

    async def sinks(self):
        """Record every beat that transfers on any sink."""
        while self.running:
            await FallingEdge(self.dut.clk)
            valid = as_int(self.dut.m_valid)
            ready = as_int(self.dut.m_ready)
            words = as_int(self.dut.m_req)
            if valid is None or ready is None or words is None:
                continue
            for d in range(N_PORTS):
                if (valid >> d) & 1 and (ready >> d) & 1:
                    beat = unpack((words >> (d * REQ_W)) & ((1 << REQ_W) - 1))
                    self.seen[d].append(beat)

    def all_sent(self):
        return [b for beats in self.sent.values() for b in beats]

    def all_seen(self):
        return [b for beats in self.seen.values() for b in beats]


def check_conservation(h: Harness):
    """Every injected beat arrives once, at the sink its dst named."""
    sent = {b.marker: b for b in h.all_sent()}
    seen_markers = [b["data"] for b in h.all_seen()]

    assert len(seen_markers) == len(set(seen_markers)), (
        "a beat was duplicated: "
        f"{len(seen_markers)} delivered, {len(set(seen_markers))} distinct"
    )

    missing = set(sent) - set(seen_markers)
    assert not missing, (
        f"{len(missing)} beat(s) never arrived, e.g. {sent[sorted(missing)[0]]}"
    )

    invented = set(seen_markers) - set(sent)
    assert not invented, f"{len(invented)} beat(s) arrived that were never sent"

    for dst, beats in h.seen.items():
        for got in beats:
            want = sent[got["data"]]
            assert dst == want.dst, (
                f"misrouted: {want} was delivered to sink {dst}"
            )
            assert got["src"] == want.src and got["tag"] == want.tag, (
                f"beat corrupted in flight: sent {want}, got {got}"
            )


def check_flow_order(h: Harness):
    """Within one source->sink flow, beats keep the order they were sent."""
    sent = {b.marker: b for b in h.all_sent()}
    for dst, beats in h.seen.items():
        per_flow = defaultdict(list)
        for got in beats:
            per_flow[sent[got["data"]].src].append(got["data"])
        for src, markers in per_flow.items():
            expected = [b.marker for b in h.sent[src] if b.dst == dst]
            assert markers == expected, (
                f"flow {src}->{dst} reordered:\n  got  {markers}\n"
                f"  want {expected}"
            )


def check_packets_not_interleaved(h: Harness):
    """At one sink, a multi-beat packet is contiguous.

    A packet is the run of beats from one source under one tag ending with
    last=1. Another source's beats appearing inside that run would leave the
    egress unable to tell the two transfers apart.
    """
    sent = {b.marker: b for b in h.all_sent()}
    for dst, beats in h.seen.items():
        open_src = None
        for got in beats:
            src = sent[got["data"]].src
            if open_src is not None and src != open_src:
                raise AssertionError(
                    f"sink {dst}: source {src} interleaved into an open "
                    f"transfer from source {open_src}"
                )
            open_src = None if got["last"] else src


@cocotb.test()
async def single_source_single_sink(dut):
    """The simplest path works before anything harder is asked of it."""
    await start(dut)
    rng = random.Random(1)
    h = Harness(dut, rng)
    cocotb.start_soon(h.sinks())

    beats = [Beat(0, 0, 7, m, int(m == 4), m) for m in range(1, 5)]
    await h.source(0, beats)
    for _ in range(20):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    h.running = False

    check_conservation(h)
    check_flow_order(h)


@cocotb.test()
async def all_sources_to_all_sinks(dut):
    """Random traffic from every source, including both engine ports."""
    await start(dut)
    rng = random.Random(0xC0FFEE)
    h = Harness(dut, rng)
    cocotb.start_soon(h.sinks())

    queues = make_packets(rng, n_packets=6 * N_SRC,
                          srcs=list(range(N_SRC)), dsts=list(range(N_PORTS)))
    drivers = [cocotb.start_soon(h.source(s, queues[s], gap=0.3))
               for s in range(N_SRC)]
    for d in drivers:
        await d
    for _ in range(60):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    h.running = False

    check_conservation(h)
    check_flow_order(h)
    check_packets_not_interleaved(h)


@cocotb.test()
async def engine_sources_are_routed(dut):
    """The primitive and block engine ports are ordinary sources.

    They sit at indices N_PORTS and N_PORTS+1 rather than among the per-port
    streams, which is exactly the kind of off-by-one worth checking explicitly.
    """
    await start(dut)
    rng = random.Random(5)
    h = Harness(dut, rng)
    cocotb.start_soon(h.sinks())

    last_sink = N_PORTS - 1
    prim = [Beat(PRIM_SRC, last_sink, 0x11, 0x1000 + i, int(i == 1), i)
            for i in range(2)]
    blk = [Beat(BLOCK_SRC, 0, 0x22, 0x2000 + i, int(i == 1), i)
           for i in range(2)]

    d0 = cocotb.start_soon(h.source(PRIM_SRC, prim))
    d1 = cocotb.start_soon(h.source(BLOCK_SRC, blk))
    await d0
    await d1
    for _ in range(30):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    h.running = False

    check_conservation(h)
    assert len(h.seen[last_sink]) == 2, "primitive engine traffic did not arrive"
    assert len(h.seen[0]) == 2, "block engine traffic did not arrive"


@cocotb.test()
async def contention_starves_no_source(dut):
    """With every source aimed at one sink, all of them make progress.

    Conservation alone would be satisfied by an arbiter that drains source 0
    completely before looking at source 1, which is a livelock waiting to
    happen once the engines share the fabric with port traffic.
    """
    await start(dut)
    rng = random.Random(9)
    h = Harness(dut, rng)
    cocotb.start_soon(h.sinks())

    per_source = 4
    queues = {
        s: [Beat(s, 0, s, (s + 1) * 1000 + i, 1, i) for i in range(per_source)]
        for s in range(N_SRC)
    }
    drivers = [cocotb.start_soon(h.source(s, queues[s])) for s in range(N_SRC)]

    # Sample progress partway through: by the time half the total traffic has
    # been accepted, every source should have moved at least one beat.
    total = per_source * N_SRC
    for _ in range(2000):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        if sum(h.accepted.values()) >= total // 2:
            break
    midpoint = dict(h.accepted)

    for d in drivers:
        await d
    for _ in range(60):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    h.running = False

    starved = [s for s in range(N_SRC) if midpoint.get(s, 0) == 0]
    assert not starved, (
        f"sources {starved} had moved nothing once half the traffic was "
        f"through: {midpoint}"
    )
    check_conservation(h)
    check_flow_order(h)


@cocotb.test()
async def backpressured_sink_blocks_only_itself(dut):
    """A sink holding m_ready low accepts nothing and loses nothing.

    Traffic to other sinks must keep flowing: one blocked egress stalling the
    whole fabric would let a slow endpoint halt every other collective.
    """
    await start(dut)
    rng = random.Random(13)
    h = Harness(dut, rng)
    cocotb.start_soon(h.sinks())

    blocked, open_sink = 0, N_PORTS - 1
    assert blocked != open_sink, "needs at least two sinks"
    dut.m_ready.value = ((1 << N_PORTS) - 1) & ~(1 << blocked)

    to_blocked = [Beat(0, blocked, 1, 0xA000 + i, 1, i) for i in range(2)]
    to_open = [Beat(1, open_sink, 2, 0xB000 + i, 1, i) for i in range(4)]

    d_open = cocotb.start_soon(h.source(1, to_open))
    cocotb.start_soon(h.source(0, to_blocked))

    await d_open
    for _ in range(40):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)

    assert not h.seen[blocked], (
        f"{len(h.seen[blocked])} beat(s) delivered to a sink holding m_ready low"
    )
    assert len(h.seen[open_sink]) == len(to_open), (
        f"a backpressured sink stalled an unrelated one: "
        f"{len(h.seen[open_sink])} of {len(to_open)} arrived"
    )

    # Release, and confirm the held traffic was queued rather than discarded.
    dut.m_ready.value = (1 << N_PORTS) - 1
    for _ in range(60):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    h.running = False

    assert h.seen[blocked], "traffic to the blocked sink was dropped, not held"
