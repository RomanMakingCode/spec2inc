"""Unit testbench for req_xbar. Contract: docs/modules.md 3.5.

The oracle is conservation, not computation: this module moves beats without
changing them, so what has to be checked is that every beat injected at a
source arrives once at the sink its dst names, unaltered, in the order its flow
sent it, without being duplicated, dropped, misrouted, or interleaved with
another packet at the same sink.

Structure: one driver coroutine per source, one monitor coroutine per sink, a
protocol checker watching the sink channels continuously, and a scoreboard
comparing what went in against what came out. Every beat carries a unique
marker so a duplicate or a swap is unambiguous rather than inferred.

Timing convention. A transfer happens at the rising edge where valid and ready
are both high, so both must be sampled in the cycle *before* that edge -- that
is, at the falling edge preceding it. Sampling after the rising edge reads the
next cycle's handshake and silently misattributes transfers.

Every test carries a timeout. Without one a DUT that never asserts ready hangs
the driver loop forever, and a hang is far less useful than a failure.
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

REQ_READ, REQ_WRITE, REQ_READ_REDUCE, REQ_WRITE_MCAST, REQ_BLOCK_INVOKE = range(5)

_FIELDS = (("op", OP_W), ("src", ID_W), ("dst", ID_W), ("tag", TAG_W),
           ("addr", ADDR_W), ("len", LEN_W), ("data", DATA_W), ("last", 1))

# Every field is compared on arrival. Checking only the routing fields would
# let a crossbar zero op/addr/len, or force last=1 on every beat -- and forcing
# last would additionally make the non-interleaving check vacuous.
_COMPARED = [name for name, _ in _FIELDS]

CLK_NS = 10
# cocotb wants a number plus a unit; a string here raises inside the
# scheduler before any test runs.
TIMEOUT_NS = CLK_NS * 4000


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

    __slots__ = ("fields",)

    def __init__(self, src, dst, tag, marker, last, seq, op=REQ_WRITE):
        self.fields = dict(op=op, src=src, dst=dst, tag=tag, addr=seq,
                           len=1, data=marker, last=last)

    def __getattr__(self, name):
        try:
            return self.fields[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @property
    def marker(self):
        return self.fields["data"]

    def word(self) -> int:
        return pack(**self.fields)

    def __repr__(self):
        f = self.fields
        return (f"Beat(src={f['src']} dst={f['dst']} tag={f['tag']} "
                f"op={f['op']} marker={f['data']:#x} last={f['last']})")


def make_packets(rng, n_packets, srcs, dsts, max_beats=3):
    """Build per-source beat queues. Marker values are globally unique."""
    ops = [REQ_READ, REQ_WRITE, REQ_READ_REDUCE, REQ_WRITE_MCAST,
           REQ_BLOCK_INVOKE]
    queues = defaultdict(list)
    marker = 1
    for _ in range(n_packets):
        src = rng.choice(srcs)
        dst = rng.choice(dsts)
        tag = rng.randrange(1 << TAG_W)
        op = rng.choice(ops)   # the fabric routes every opcode identically
        n_beats = rng.randint(1, max_beats)
        for i in range(n_beats):
            queues[src].append(
                Beat(src, dst, tag, marker, int(i == n_beats - 1), i, op=op))
            marker += 1
    return queues


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
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


class Harness:
    """Drives every source, monitors every sink, and records what happened."""

    def __init__(self, dut, rng):
        self.dut = dut
        self.rng = rng
        self.seen = defaultdict(list)     # sink -> [unpacked beat]
        self.sent = defaultdict(list)     # source -> [Beat]
        self.accepted = defaultdict(int)  # source -> beats accepted
        self.running = True
        self.protocol_errors = []
        # Cycles where the sink bus could not be read at all. Skipping these
        # silently would hide both deliveries and protocol violations, so they
        # are counted and reported rather than ignored.
        self.blind_cycles = 0
        self.offered_while_blocked = False
        # Lets a test stop every driver and release the bus. Without it the
        # source coroutines keep asserting s_valid, and a check for "the
        # fabric is quiet" would really be demanding that outputs are gated
        # during reset -- behaviour the spec does not require.
        self.halt = False
        self._words = [0] * N_SRC
        self._valid = [0] * N_SRC

    def _drive(self):
        packed = 0
        for s in range(N_SRC):
            packed |= self._words[s] << (s * REQ_W)
        self.dut.s_req.value = packed
        self.dut.s_valid.value = sum(v << s for s, v in enumerate(self._valid))

    async def source(self, idx, beats, gap=0.0):
        """Present each beat, holding it until the cycle it is accepted in.

        s_ready is sampled at the falling edge *before* the rising edge that
        would transfer, so the beat is recorded as sent in the same cycle the
        DUT takes it.
        """
        # Align to just after a rising edge. Everything below keeps that
        # phase, so a beat is always presented at the start of a cycle and the
        # first s_ready sample belongs to that same cycle. Presenting
        # mid-cycle and then awaiting the *next* falling edge would skip the
        # cycle the DUT may already have accepted in, leaving the accepted
        # word on the bus to be delivered a second time.
        await RisingEdge(self.dut.clk)

        for beat in beats:
            if self.halt:
                break
            while gap and self.rng.random() < gap:
                await FallingEdge(self.dut.clk)
                await RisingEdge(self.dut.clk)

            self._words[idx] = beat.word()
            self._valid[idx] = 1
            self._drive()

            while True:
                await FallingEdge(self.dut.clk)
                ready = as_int(self.dut.s_ready)
                taken = ready is not None and (ready >> idx) & 1
                await RisingEdge(self.dut.clk)
                if taken or self.halt:
                    break
            if self.halt:
                break
            self.sent[idx].append(beat)
            self.accepted[idx] += 1

            # Drop valid the instant the beat is taken. Leaving the accepted
            # word on the bus during the idle gap below would re-offer it, and
            # a correct DUT would dutifully deliver it again -- reported as the
            # DUT duplicating beats when the testbench sent them twice.
            self._valid[idx] = 0
            self._drive()

        self._valid[idx] = 0
        self._drive()

    async def sinks(self):
        """Record every beat that transfers on any sink."""
        while self.running:
            await FallingEdge(self.dut.clk)
            valid = as_int(self.dut.m_valid)
            ready = as_int(self.dut.m_ready)
            words = as_int(self.dut.m_req)
            if valid is None or ready is None or words is None:
                self.blind_cycles += 1
                await RisingEdge(self.dut.clk)
                continue
            for d in range(N_PORTS):
                if (valid >> d) & 1 and (ready >> d) & 1:
                    self.seen[d].append(
                        unpack((words >> (d * REQ_W)) & ((1 << REQ_W) - 1)))
            await RisingEdge(self.dut.clk)

    async def protocol(self):
        """Channel rules 2 and 3, watched continuously on every sink.

        A beat offered on a sink must stay offered, unchanged, until it is
        taken. Without this a design that re-arbitrates every cycle -- swapping
        the beat out from under a sink that has not accepted it yet -- passes
        every conservation check, because the swapped-out beat is simply
        delivered later.
        """
        held = {}
        while self.running:
            await FallingEdge(self.dut.clk)
            valid = as_int(self.dut.m_valid)
            ready = as_int(self.dut.m_ready)
            words = as_int(self.dut.m_req)
            if valid is None or ready is None or words is None:
                await RisingEdge(self.dut.clk)
                continue
            for d in range(N_PORTS):
                v = (valid >> d) & 1
                word = (words >> (d * REQ_W)) & ((1 << REQ_W) - 1)
                if d in held:
                    if not v:
                        self.protocol_errors.append(
                            f"sink {d}: m_valid dropped before m_ready")
                        del held[d]
                    elif word != held[d]:
                        self.protocol_errors.append(
                            f"sink {d}: m_req changed while waiting for m_ready")
                        held[d] = word
                if v and not (ready >> d) & 1:
                    self.offered_while_blocked = True
                    held.setdefault(d, word)
                elif v and (ready >> d) & 1:
                    held.pop(d, None)
            await RisingEdge(self.dut.clk)

    async def drain(self, cycles=80):
        for _ in range(cycles):
            await RisingEdge(self.dut.clk)
            await FallingEdge(self.dut.clk)
        self.running = False
        await RisingEdge(self.dut.clk)

    def all_sent(self):
        return [b for beats in self.sent.values() for b in beats]

    def all_seen(self):
        return [b for beats in self.seen.values() for b in beats]


def check_protocol(h: Harness):
    assert not h.protocol_errors, (
        f"{len(h.protocol_errors)} channel-protocol violation(s), e.g. "
        f"{h.protocol_errors[0]}"
    )


def check_conservation(h: Harness):
    """Every injected beat arrives once, unaltered, at the sink dst named."""
    sent = {b.marker: b for b in h.all_sent()}
    seen_markers = [b["data"] for b in h.all_seen()]

    assert len(seen_markers) == len(set(seen_markers)), (
        f"a beat was duplicated: {len(seen_markers)} delivered, "
        f"{len(set(seen_markers))} distinct"
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
            assert dst == want.dst, f"misrouted: {want} delivered to sink {dst}"
            for field in _COMPARED:
                assert got[field] == want.fields[field], (
                    f"{want}: field '{field}' changed in flight -- "
                    f"got {got[field]}, sent {want.fields[field]}"
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
    """At one sink, a multi-beat packet is contiguous."""
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


def check_all(h: Harness):
    assert h.blind_cycles == 0, (
        f"{h.blind_cycles} cycle(s) where the sink bus read as X/Z, so both "
        "deliveries and protocol violations went unobserved in them"
    )
    check_protocol(h)
    check_conservation(h)
    check_flow_order(h)
    check_packets_not_interleaved(h)


def spawn(h: Harness):
    cocotb.start_soon(h.sinks())
    cocotb.start_soon(h.protocol())


@cocotb.test(timeout_time=TIMEOUT_NS, timeout_unit="ns")
async def quiet_after_reset(dut):
    """Out of reset nothing is offered and nothing is accepted."""
    await start(dut)
    for _ in range(5):
        valid = as_int(dut.m_valid)
        assert valid == 0, f"m_valid={valid} out of reset with no traffic"
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)


@cocotb.test(timeout_time=TIMEOUT_NS, timeout_unit="ns")
async def single_source_single_sink(dut):
    """The simplest path works before anything harder is asked of it."""
    await start(dut)
    h = Harness(dut, random.Random(1))
    spawn(h)

    beats = [Beat(0, 0, 7, m, int(m == 4), m) for m in range(1, 5)]
    await h.source(0, beats)
    await h.drain(30)

    check_all(h)


@cocotb.test(timeout_time=TIMEOUT_NS, timeout_unit="ns")
async def all_sources_to_all_sinks(dut):
    """Random traffic from every source, including both engine ports."""
    await start(dut)
    rng = random.Random(0xC0FFEE)
    h = Harness(dut, rng)
    spawn(h)

    queues = make_packets(rng, n_packets=6 * N_SRC,
                          srcs=list(range(N_SRC)), dsts=list(range(N_PORTS)))
    for d in [cocotb.start_soon(h.source(s, queues[s], gap=0.3))
              for s in range(N_SRC)]:
        await d
    await h.drain()

    check_all(h)


@cocotb.test(timeout_time=TIMEOUT_NS, timeout_unit="ns")
async def engine_sources_are_routed(dut):
    """The primitive and block engine ports are ordinary sources.

    They sit at indices N_PORTS and N_PORTS+1 rather than among the per-port
    streams, which is exactly the kind of off-by-one worth checking explicitly.
    """
    await start(dut)
    h = Harness(dut, random.Random(5))
    spawn(h)

    last_sink = N_PORTS - 1
    prim = [Beat(PRIM_SRC, last_sink, 0x11, 0x1000 + i, int(i == 1), i)
            for i in range(2)]
    blk = [Beat(BLOCK_SRC, 0, 0x22, 0x2000 + i, int(i == 1), i)
           for i in range(2)]

    d0 = cocotb.start_soon(h.source(PRIM_SRC, prim))
    d1 = cocotb.start_soon(h.source(BLOCK_SRC, blk))
    await d0
    await d1
    await h.drain(40)

    check_all(h)
    assert len(h.seen[last_sink]) == 2, "primitive engine traffic did not arrive"
    assert len(h.seen[0]) == 2, "block engine traffic did not arrive"


@cocotb.test(timeout_time=TIMEOUT_NS, timeout_unit="ns")
async def a_greedy_source_cannot_starve_another(dut):
    """A source offering continuously must not lock another out forever.

    This is the spec's actual requirement -- no source starved indefinitely --
    and deliberately not a share-of-bandwidth test. Demanding equal shares
    would reject legitimate policies the spec permits, such as a hierarchical
    arbiter that round-robins the unicast ports as a group against the two
    engines. What is not permitted is a victim never finishing.

    Progress is measured at the sink, not at source acceptance: an input skid
    buffer lets a crossbar accept from every source immediately and still
    drain only one of them, which a source-side count would score as fair.
    """
    await start(dut)
    h = Harness(dut, random.Random(9))
    spawn(h)

    victim, greedy = 1, 0
    victim_beats = [Beat(victim, 0, 0x5A, 0x7000 + i, 1, i) for i in range(4)]

    async def greedy_forever():
        i = 0
        while h.running:
            await h.source(greedy, [Beat(greedy, 0, 0x0B, 0x9000 + i, 1, 0)])
            i += 1

    cocotb.start_soon(greedy_forever())
    await RisingEdge(dut.clk)

    delivered = cocotb.start_soon(h.source(victim, victim_beats))
    for _ in range(1500):
        if delivered.done():
            break
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)

    assert delivered.done(), (
        f"source {victim} never got its {len(victim_beats)} beats through "
        f"while source {greedy} offered continuously -- it is starved"
    )

    await h.drain()
    from_victim = [b for b in h.seen[0]
                   if b["src"] == victim and b["tag"] == 0x5A]
    assert len(from_victim) == len(victim_beats), (
        f"{len(from_victim)} of {len(victim_beats)} victim beats reached the "
        "sink, so acceptance was not delivery"
    )
    check_protocol(h)


@cocotb.test(timeout_time=TIMEOUT_NS, timeout_unit="ns")
async def backpressured_sink_blocks_only_itself(dut):
    """A sink holding m_ready low accepts nothing and loses nothing.

    Traffic to other sinks must keep flowing: one blocked egress stalling the
    whole fabric would let a slow endpoint halt every other collective.
    """
    await start(dut)
    h = Harness(dut, random.Random(13))
    spawn(h)

    blocked, open_sink = 0, N_PORTS - 1
    assert blocked != open_sink, "needs at least two sinks"
    # Written at the start of a cycle, matching the harness convention, so it
    # cannot race the monitors sampling mid-cycle.
    await RisingEdge(dut.clk)
    dut.m_ready.value = ((1 << N_PORTS) - 1) & ~(1 << blocked)

    to_blocked = [Beat(0, blocked, 1, 0xA000 + i, int(i == 2), i)
                  for i in range(3)]
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
    # The beat must still be *offered* to the stalled sink. Without this, a
    # design computing m_valid[d] = have_beat && m_ready[d] passes every other
    # test: it simply never enters the offered-but-not-accepted state, so the
    # protocol monitor never sees anything to complain about. That gating is
    # also exactly the valid-depends-on-ready direction channel rule 4 forbids.
    assert h.offered_while_blocked, (
        "nothing was ever offered to the backpressured sink: m_valid never "
        "rose while m_ready was low, so valid is gated by ready"
    )
    assert len(h.seen[open_sink]) == len(to_open), (
        f"a backpressured sink stalled an unrelated one: "
        f"{len(h.seen[open_sink])} of {len(to_open)} arrived"
    )

    # Release at the start of a cycle, again to avoid racing the monitors.
    await RisingEdge(dut.clk)
    dut.m_ready.value = (1 << N_PORTS) - 1
    await h.drain()

    assert len(h.seen[blocked]) == len(to_blocked), (
        f"held traffic was dropped: {len(h.seen[blocked])} of "
        f"{len(to_blocked)} arrived after m_ready was released"
    )
    check_all(h)


@cocotb.test(timeout_time=TIMEOUT_NS, timeout_unit="ns")
async def sink_ready_toggling_preserves_traffic(dut):
    """Randomly toggling every sink's m_ready loses and corrupts nothing.

    This is what exercises channel rule 3 in anger: a design that re-arbitrates
    while a sink is mid-handshake changes the offered beat, which the protocol
    monitor sees even when the beat is eventually delivered anyway.
    """
    await start(dut)
    rng = random.Random(21)
    h = Harness(dut, rng)
    spawn(h)

    stop_chaos = []

    async def chaos():
        # Change m_ready just after a rising edge so it is stable through the
        # falling-edge sample and the next rising edge. Driving it at the
        # falling edge races the monitors reading it in that same timestep.
        while h.running and not stop_chaos:
            await RisingEdge(dut.clk)
            dut.m_ready.value = rng.getrandbits(N_PORTS)
            await FallingEdge(dut.clk)
        dut.m_ready.value = (1 << N_PORTS) - 1

    cocotb.start_soon(chaos())
    queues = make_packets(rng, n_packets=4 * N_SRC,
                          srcs=list(range(N_SRC)), dsts=list(range(N_PORTS)))
    for d in [cocotb.start_soon(h.source(s, queues[s], gap=0.2))
              for s in range(N_SRC)]:
        await d

    # Stop the chaos generator and let it observe the flag before opening
    # every sink, otherwise it keeps randomising m_ready through the drain and
    # the tail finishes under roughly half ready -- a flake that surfaces as a
    # beat having "never arrived".
    stop_chaos.append(True)
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.m_ready.value = (1 << N_PORTS) - 1
    await h.drain()

    check_all(h)


@cocotb.test(timeout_time=TIMEOUT_NS, timeout_unit="ns")
async def reset_mid_traffic_clears_the_fabric(dut):
    """Reset asserted with beats in flight silences the fabric and recovers.

    `quiet_after_reset` on its own proves very little: verilator is two-state,
    so unreset registers read zero and a design that never connects rst_n
    passes it. Asserting reset while transfers are actually in progress is what
    distinguishes a fabric that flushes from one that carries stale state
    across the reset and keeps delivering it afterwards.
    """
    await start(dut)
    rng = random.Random(31)
    h = Harness(dut, rng)
    spawn(h)

    # Get several sources genuinely busy, aimed at one sink so beats queue.
    for s_idx in range(min(4, N_SRC)):
        cocotb.start_soon(h.source(
            s_idx, [Beat(s_idx, 0, 0x33, 0xC000 + s_idx * 16 + i, int(i == 2), i)
                    for i in range(3)]))
    for _ in range(6):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)

    # Quiesce the sources and let them release the bus, so what follows tests
    # that no state survives reset rather than that outputs are gated during
    # it -- the latter is not in the spec.
    h.halt = True
    for _ in range(3):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    h.running = False          # stop the monitors before the state disappears
    await RisingEdge(dut.clk)

    dut.rst_n.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        valid = as_int(dut.m_valid)
        assert valid == 0, (
            f"m_valid={valid} while rst_n is low -- the fabric kept offering "
            "beats through reset"
        )

    await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)

    # And it still works afterwards, with none of the pre-reset traffic
    # reappearing.
    h2 = Harness(dut, rng)
    spawn(h2)
    await h2.source(0, [Beat(0, 1, 0x44, 0xD000 + i, int(i == 1), i)
                        for i in range(2)])
    await h2.drain(40)

    check_all(h2)
    assert len(h2.seen[1]) == 2, (
        f"the fabric did not recover after reset: {len(h2.seen[1])} of 2 "
        "post-reset beats arrived"
    )
    stale = [b for beats in h2.seen.values() for b in beats
             if b["tag"] == 0x33]
    assert not stale, (
        f"{len(stale)} pre-reset beat(s) were delivered after reset -- "
        "in-flight state survived it"
    )
