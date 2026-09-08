"""Reference model for collective semantics.

FROZEN TRUST ANCHOR. This module defines what the three collectives *mean*,
independently of any RTL. It is the root of the verification trust chain: every
integration-level check is ultimately compared against this file, so design and
verification agents may not edit it. It is deliberately small enough to review
by eye and to validate by hand.

The semantics here are derived from the definition of each collective, not from
the switch's implementation of it. That independence is the point -- a reference
model written by reading the RTL would agree with the RTL's bugs.

Arithmetic matches the hardware: reduction is integer addition over
RED_LANE_W-bit lanes, wrapping on overflow, with no saturation and no flags.
Because that operation is associative and commutative, the result does not
depend on the order members are combined in -- which is what permits the block
engine to collect member reads in whatever order they arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import numpy as np

# Mirrors spec2inc_pkg::RED_LANE_W. A buffer is modeled as a 1-D array of lanes.
LANE_BITS = 32
LANE_DTYPE = np.uint32
LANE_MASK = (1 << LANE_BITS) - 1

# Mirrors spec2inc_pkg::DATA_W / RED_LANE_W.
LANES_PER_BLOCK = 8


class CollKind(IntEnum):
    """Values match spec2inc_pkg::coll_type_e."""

    BCAST = 0
    REDUCE = 1
    ALLREDUCE = 2


@dataclass(frozen=True)
class Collective:
    kind: CollKind
    members: tuple[int, ...]
    root: int

    def __post_init__(self) -> None:
        if self.root not in self.members:
            raise ValueError(f"root {self.root} not in members {self.members}")
        if len(set(self.members)) != len(self.members):
            raise ValueError(f"duplicate members: {self.members}")
        if len(self.members) < 2:
            raise ValueError("a collective needs at least two members")


def reduce_lanes(buffers: list[np.ndarray]) -> np.ndarray:
    """Elementwise sum of equal-length lane buffers, wrapping at LANE_BITS.

    Accumulates in uint64 and masks after each add so the wrap is explicit
    rather than relying on numpy's overflow behavior.
    """
    if not buffers:
        raise ValueError("nothing to reduce")
    length = len(buffers[0])
    if any(len(b) != length for b in buffers):
        raise ValueError("buffers differ in length")

    acc = np.zeros(length, dtype=np.uint64)
    for buf in buffers:
        acc = (acc + buf.astype(np.uint64)) & LANE_MASK
    return acc.astype(LANE_DTYPE)


def expected_outputs(
    coll: Collective,
    in_bufs: dict[int, np.ndarray],
) -> dict[int, np.ndarray | None]:
    """Expected output-buffer contents for every endpoint after `coll`.

    `in_bufs` maps endpoint ID to that endpoint's input buffer. It must contain
    an entry for every member; entries for non-members are ignored.

    Returns a map over the same endpoints as `in_bufs`. A value of ``None``
    means that endpoint's output buffer must be **bit-unchanged** -- which is
    how the non-member-untouched property is expressed, rather than as a
    separate rule a checker has to remember to apply.
    """
    missing = [m for m in coll.members if m not in in_bufs]
    if missing:
        raise ValueError(f"no input buffer for members {missing}")

    out: dict[int, np.ndarray | None] = {ep: None for ep in in_bufs}

    if coll.kind == CollKind.BCAST:
        # Root's buffer is copied to every member, root included.
        payload = in_bufs[coll.root]
        for m in coll.members:
            out[m] = payload.copy()

    elif coll.kind == CollKind.REDUCE:
        # All members contribute; only the root's output is written.
        out[coll.root] = reduce_lanes([in_bufs[m] for m in coll.members])

    elif coll.kind == CollKind.ALLREDUCE:
        # All members contribute and all members receive.
        result = reduce_lanes([in_bufs[m] for m in coll.members])
        for m in coll.members:
            out[m] = result.copy()

    else:
        raise ValueError(f"unhandled collective kind: {coll.kind}")

    return out


def _self_test() -> None:
    """Hand-computed cases. Run with `python verif/reference.py`.

    These are checked against values worked out by hand rather than against
    another implementation, so this validates the anchor itself.
    """
    L = LANE_DTYPE

    bufs = {
        0: np.array([1, 2, 3, 4], dtype=L),
        1: np.array([10, 20, 30, 40], dtype=L),
        2: np.array([100, 200, 300, 400], dtype=L),
        3: np.array([1000, 2000, 3000, 4000], dtype=L),
    }
    members = (0, 1, 2, 3)

    # BROADCAST from rank 1: every member ends up with rank 1's buffer.
    got = expected_outputs(Collective(CollKind.BCAST, members, root=1), bufs)
    for m in members:
        assert np.array_equal(got[m], bufs[1]), f"bcast rank {m}: {got[m]}"

    # REDUCE to rank 0: only rank 0 written, others untouched.
    got = expected_outputs(Collective(CollKind.REDUCE, members, root=0), bufs)
    assert np.array_equal(got[0], np.array([1111, 2222, 3333, 4444], dtype=L))
    for m in (1, 2, 3):
        assert got[m] is None, f"reduce should not write rank {m}"

    # ALL-REDUCE: every member gets the same sum.
    got = expected_outputs(Collective(CollKind.ALLREDUCE, members, root=0), bufs)
    for m in members:
        assert np.array_equal(got[m], np.array([1111, 2222, 3333, 4444], dtype=L))

    # Non-members are reported as untouched.
    with_extra = {**bufs, 7: np.array([9, 9, 9, 9], dtype=L)}
    got = expected_outputs(Collective(CollKind.ALLREDUCE, members, root=0), with_extra)
    assert got[7] is None, "non-member must be untouched"

    # Lane arithmetic wraps at 2**32 rather than saturating or widening.
    big = {
        0: np.array([2**32 - 1], dtype=L),
        1: np.array([1], dtype=L),
    }
    got = expected_outputs(Collective(CollKind.ALLREDUCE, (0, 1), root=0), big)
    assert np.array_equal(got[0], np.array([0], dtype=L)), f"wrap: {got[0]}"

    # Order independence -- the property that lets the block engine collect
    # member responses in arbitrary order.
    fwd = reduce_lanes([bufs[m] for m in members])
    rev = reduce_lanes([bufs[m] for m in reversed(members)])
    assert np.array_equal(fwd, rev)

    # Malformed collectives are rejected rather than silently accepted.
    for bad in (
        lambda: Collective(CollKind.REDUCE, (0, 1), root=5),
        lambda: Collective(CollKind.REDUCE, (0, 0), root=0),
        lambda: Collective(CollKind.REDUCE, (0,), root=0),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("malformed collective was accepted")

    print("reference model self-test OK")


if __name__ == "__main__":
    _self_test()
