# Spec2INC Module Specifications

Per-submodule specification: interface, responsibility, and local correctness contract for
each block in [architecture.md](architecture.md). Every module here is specified against the
**complete** architecture, not against an incremental feature subset, so interfaces do not need
rework as engines land.

Still deliberately excluded: pipeline stage counts, FSM state encodings, buffer sizing, and
arbitration policy internals. Those are microarchitecture and belong to whoever (or whatever)
implements the module.

## 1. Why this decomposition

Two properties are being bought here.

**Interfaces settled against full requirements.** Every module's ports are defined knowing that
both the primitive and block engines exist. Nothing gets retrofitted when the second mechanism
arrives.

**Local oracles.** Most modules can be checked without knowing what a collective *means*. A
reduction engine is a pure function; a crossbar either delivers the packet or does not; an issue
limiter either exceeds its cap or does not. Only integration needs the collective-semantics
oracle. This matters for the agent loop: tight, fast, unambiguous per-module feedback is what
makes agentic optimization tractable, and a vague whole-switch target is what makes it fail.

## 2. Shared package — `spec2inc_pkg`

The contract every module references. This package **is the read-only architectural contract**
handed to the design agent; the agent may edit module bodies, never this file.

### 2.1 Constants

| Name | Value | Meaning |
| --- | --- | --- |
| `MAX_PORTS` | 64 | Upper bound on radix. Fixes mask widths so types stay unparameterized. |
| `DATA_W` | 256 | Datapath width, bits per beat. |
| `ADDR_W` | 48 | Address width. |
| `TAG_W` | 8 | Transaction tag width. |
| `ID_W` | 6 | Endpoint / group ID width (`$clog2(MAX_PORTS)`). |
| `LEN_W` | 16 | Transfer length in beats. |
| `RED_LANE_W` | 32 | Reduction lane width; `DATA_W/RED_LANE_W` = 8 lanes per beat. |

Widths are package **localparams**, not module parameters. Packed struct fields need
elaboration-time constants, and parameterized types are exactly the SystemVerilog that Yosys
handles worst. `N_PORTS` remains a module parameter and is swept; masks are always `MAX_PORTS`
wide with only the low `N_PORTS` bits meaningful.

### 2.2 Types

```systemverilog
typedef enum logic [2:0] {
    REQ_READ         = 3'd0,   // unicast read, or switch-initiated member read
    REQ_WRITE        = 3'd1,   // unicast write, or switch-initiated member write
    REQ_READ_REDUCE  = 3'd2,   // collective primitive
    REQ_WRITE_MCAST  = 3'd3,   // collective primitive
    REQ_BLOCK_INVOKE = 3'd4    // block collective descriptor delivery
} req_op_e;

typedef struct packed {
    req_op_e            op;
    logic [ID_W-1:0]    src;    // requesting endpoint; stamped by port_ingress
    logic [ID_W-1:0]    dst;    // endpoint ID, or group ID for primitives
    logic [TAG_W-1:0]   tag;    // endpoint-assigned, or switch-assigned when switch-initiated
    logic [ADDR_W-1:0]  addr;
    logic [LEN_W-1:0]   len;    // total beats in this transfer
    logic [DATA_W-1:0]  data;   // write payload, or descriptor beat
    logic               last;   // final beat of the transfer
} req_t;

typedef enum logic [1:0] { RSP_OK = 2'd0, RSP_ERR = 2'd1 } rsp_status_e;

typedef enum logic [1:0] {
    DEST_ENDPOINT  = 2'd0,     // response returns to a requesting endpoint
    DEST_PRIMITIVE = 2'd1,     // response is consumed by the primitive engine
    DEST_BLOCK     = 2'd2      // response is consumed by the block engine
} rsp_dest_e;

typedef struct packed {
    rsp_dest_e          dest_class;
    logic [ID_W-1:0]    dst;    // endpoint index when dest_class == DEST_ENDPOINT
    logic [ID_W-1:0]    src;    // responding endpoint
    logic [TAG_W-1:0]   tag;
    rsp_status_e        status;
    logic [DATA_W-1:0]  data;
    logic               last;
} rsp_t;

typedef enum logic [1:0] {
    COLL_BCAST     = 2'd0,
    COLL_REDUCE    = 2'd1,
    COLL_ALLREDUCE = 2'd2
} coll_type_e;

typedef struct packed {          // delivered in the data payload of REQ_BLOCK_INVOKE
    coll_type_e         coll;
    logic [ID_W-1:0]    group;
    logic [ADDR_W-1:0]  in_off;
    logic [ADDR_W-1:0]  out_off;
    logic [ADDR_W-1:0]  status_off;
    logic [LEN_W-1:0]   n_blocks;
} block_desc_t;                  // 168 bits, fits in one DATA_W beat
```

**Simplification on record:** header fields are carried on every beat rather than split into
separate address and data channels. Real designs separate them; combining keeps each
interconnect a single network and roughly halves the module count. It does not affect the
primitive-vs-block comparison, since both mechanisms pay the same overhead.

### 2.3 Channel convention

All request and response channels use ready/valid with these rules, checked by assertion
wherever a channel appears:

1. Transfer occurs on `valid && ready`.
2. Once `valid` is asserted it must remain asserted until transfer completes.
3. Payload must be stable while `valid && !ready`.
4. **`ready` must not combinationally depend on `valid` of the same channel** — this prevents
   combinational loops closing through the crossbars.
5. Multi-beat transfers from one source to one destination must not interleave with another
   transfer on the same channel.

## 3. DUT modules

### 3.1 `reduction_engine`

**Responsibility.** Combine up to `N_PORTS` operand beats into one, lane-wise, as integer
addition over `RED_LANE_W`-bit lanes. Shared between the primitive and block engines through an
arbiter; `RED_SHARED=0` instantiates one per client instead.

| Port | Dir | Type | Notes |
| --- | --- | --- | --- |
| `clk`, `rst_n` | in | `logic` | |
| `op_valid` | in | `logic` | Operand set presented |
| `op_ready` | out | `logic` | |
| `op_mask` | in | `logic [N_PORTS-1:0]` | Which operand slots are populated |
| `op_data` | in | `logic [N_PORTS*DATA_W-1:0]` | Operand beats, operand *p* at `[p*DATA_W +: DATA_W]` |
| `op_id` | in | `logic [TAG_W-1:0]` | Opaque, returned with the result |
| `res_valid` | out | `logic` | |
| `res_ready` | in | `logic` | |
| `res_data` | out | `logic [DATA_W-1:0]` | |
| `res_id` | out | `logic [TAG_W-1:0]` | Matches `op_id` |

**Contract.**
- `res_data` lane *i* equals the sum of lane *i* across all operands whose mask bit is set.
- Result appears exactly `RED_LATENCY` cycles after acceptance, and `res_id` matches.
- Order-independence: any permutation of operands over the same mask yields identical output
  (guaranteed by integer add being associative and commutative — this is the property that lets
  the block engine collect member reads in arbitrary order).
- Mask with one bit set is a pass-through. Mask of zero is illegal.
- Lane overflow wraps; no saturation, no flags.

**Parameters:** `N_PORTS`, `RED_LATENCY`.

**Oracle:** pure function. Randomized operand sets checked against a Python sum. No collective
semantics involved.

Sized by `N_PORTS` rather than `MAX_PORTS`: this engine only ever sees its own instance's
operands, so a fixed 64-slot port would carry dead bits at every smaller radix. Callers holding
a `port_mask_t` slice it down. `op_data` is flat rather than a packed 2-D array so its layout is
unambiguous to verilator, sv2v, and the cocotb testbench alike.

---

### 3.2 `group_table`

**Responsibility.** Per-port storage of group membership. One instance inside each
`port_ingress`.

| Port | Dir | Type | Notes |
| --- | --- | --- | --- |
| `clk`, `rst_n` | in | `logic` | |
| `wr_en` | in | `logic` | Testbench-driven preload |
| `wr_idx` | in | `logic [ID_W-1:0]` | |
| `wr_valid_bit` | in | `logic` | |
| `wr_members` | in | `logic [MAX_PORTS-1:0]` | |
| `rd_idx` | in | `logic [ID_W-1:0]` | |
| `rd_valid_bit` | out | `logic` | |
| `rd_members` | out | `logic [MAX_PORTS-1:0]` | |

**Contract.**
- Read-after-write returns the written entry.
- Entries are independent; writing one never disturbs another.
- Reset clears all valid bits.
- Storage is an explicit register array, never an associative array — required for the Yosys
  elaboration gate.

**Parameters:** `N_PORTS`, `GROUP_TABLE_ENTRIES`.

**Oracle:** write/read-back model. Trivial.

---

### 3.3 `port_ingress`

**Responsibility.** Accept endpoint-initiated requests on one port, stamp the source ID, and
route each to exactly one of three destinations: unicast to the request crossbar, primitive to
the primitive engine (with resolved member mask), or descriptor to the block engine. Owns this
port's `group_table`.

| Port | Dir | Type | Notes |
| --- | --- | --- | --- |
| `clk`, `rst_n` | in | `logic` | |
| `port_id` | in | `logic [ID_W-1:0]` | This port's endpoint ID |
| `req_in_*` | in | `req_t` + v/r | From endpoint |
| `uni_*` | out | `req_t` + v/r | To request crossbar |
| `prim_*` | out | `req_t` + v/r | To primitive engine |
| `prim_members` | out | `logic [MAX_PORTS-1:0]` | Resolved mask, valid with `prim_valid` |
| `blk_*` | out | `req_t` + v/r | To block engine |
| `err_*` | out | `rsp_t` + v/r | Immediate error response for rejected requests |
| `gt_wr_*` | in | — | Group table preload passthrough |

**Contract.**
- Each accepted request is emitted on exactly one of `uni` / `prim` / `blk` / `err` — never zero,
  never more than one.
- Routing follows `op`: `REQ_READ`/`REQ_WRITE` → `uni`; `REQ_READ_REDUCE`/`REQ_WRITE_MCAST` →
  `prim`; `REQ_BLOCK_INVOKE` → `blk`.
- `src` is overwritten with `port_id` regardless of what the endpoint supplied. An endpoint
  cannot forge another endpoint's identity.
- A primitive request is rejected to `err` when: the group's valid bit is clear, the requesting
  port is not a member, or the mask has fewer than two members.
- `prim_members` reflects the group table entry for `dst` at the cycle `prim_valid` asserts.
- Backpressure on any output path backpressures `req_in_ready`; no request is dropped silently.

**Parameters:** `N_PORTS`, `GROUP_TABLE_ENTRIES`.

**Oracle:** per-request classification table plus rejection cases. No collective semantics.

---

### 3.4 `port_egress`

**Responsibility.** Arbitrate among the three request sources targeting this port and enforce
the issue-rate limit. Also forwards responses outbound to the endpoint.

| Port | Dir | Type | Notes |
| --- | --- | --- | --- |
| `clk`, `rst_n` | in | `logic` | |
| `xbar_req_*` | in | `req_t` + v/r | From request crossbar (already arbitrated per output) |
| `req_out_*` | out | `req_t` + v/r | To endpoint |
| `xbar_rsp_*` | in | `rsp_t` + v/r | From response crossbar |
| `rsp_out_*` | out | `rsp_t` + v/r | To endpoint |
| `issued_cnt` | out | `logic [15:0]` | Instrumentation |
| `stall_cnt` | out | `logic [15:0]` | Instrumentation |

**Contract.**
- **No more than `PORT_ISSUE_W` requests are issued in any single cycle.** This is the assertion
  that keeps block collectives from fanning out for free; it is checked continuously, not
  sampled.
- No request is dropped: every accepted input eventually appears on `req_out`.
- Multi-beat transfers are not interleaved with other transfers.
- Counters increment on issue and on backpressured cycles respectively.

**Parameters:** `PORT_ISSUE_W`.

**Oracle:** cycle-by-cycle issue count assertion, plus conservation (inputs accepted == outputs
emitted). No collective semantics.

---

### 3.5 `req_xbar`

**Responsibility.** Route request beats from any source to the addressed port egress, with
per-output arbitration.

Sources: `N_PORTS` unicast streams from `port_ingress`, one from `primitive_engine`, one from
`block_engine`. Sinks: `N_PORTS` port egresses.

| Port | Dir | Type | Notes |
| --- | --- | --- | --- |
| `clk`, `rst_n` | in | `logic` | |
| `s_req_*` | in | `req_t[N_PORTS+2]` + v/r | Sources; indices `N_PORTS`, `N_PORTS+1` are the engines |
| `m_req_*` | out | `req_t[N_PORTS]` + v/r | Sinks |

**Contract.**
- A beat presented at source *s* with `dst == d` is delivered to sink *d* exactly once —
  never dropped, never duplicated, never misrouted.
- Beats within one source→sink flow keep their order. No ordering guarantee across flows.
- No source is starved indefinitely under continuous contention.
- Multi-beat transfers from one source to one sink are not interleaved with another source's
  transfer to that same sink.

**Parameters:** `N_PORTS`.

**Oracle:** tagged-packet conservation. Inject uniquely tagged beats from all sources, assert
every one arrives at its addressed sink exactly once and in per-flow order. No collective
semantics.

---

### 3.6 `rsp_xbar`

**Responsibility.** Route response beats from port ingress back to their consumer, selected by
`dest_class`.

Sources: `N_PORTS` response streams from endpoints. Sinks: `N_PORTS` port egresses, plus the
primitive engine and block engine.

| Port | Dir | Type | Notes |
| --- | --- | --- | --- |
| `clk`, `rst_n` | in | `logic` | |
| `s_rsp_*` | in | `rsp_t[N_PORTS]` + v/r | From endpoints |
| `m_rsp_*` | out | `rsp_t[N_PORTS]` + v/r | To port egress |
| `prim_rsp_*` | out | `rsp_t` + v/r | To primitive engine |
| `blk_rsp_*` | out | `rsp_t` + v/r | To block engine |

**Contract.** As `req_xbar`, with sink selected by `dest_class`: `DEST_ENDPOINT` → `m_rsp[dst]`,
`DEST_PRIMITIVE` → `prim_rsp`, `DEST_BLOCK` → `blk_rsp`.

**Independence requirement:** this network must make forward progress regardless of the state of
`req_xbar`. It shares no buffering, no arbiter, and no backpressure path with it. This is the
deadlock-avoidance property from architecture.md §8 and is the reason the two crossbars are
separate modules rather than one parameterized instance.

**Parameters:** `N_PORTS`.

**Oracle:** as `req_xbar`, plus a directed test that saturates `req_xbar` and asserts `rsp_xbar`
still drains.

---

### 3.7 `primitive_engine`

**Responsibility.** Execute one collective primitive: replicate the request to every group
member, track outstanding member responses, reduce them, and return a single response to the
requester.

| Port | Dir | Type | Notes |
| --- | --- | --- | --- |
| `clk`, `rst_n` | in | `logic` | |
| `prim_*` | in | `req_t` + v/r | From any `port_ingress` (arbitrated upstream) |
| `prim_members` | in | `logic [MAX_PORTS-1:0]` | |
| `fanout_*` | out | `req_t` + v/r | To `req_xbar` |
| `rsp_in_*` | in | `rsp_t` + v/r | Member responses from `rsp_xbar` |
| `rsp_out_*` | out | `rsp_t` + v/r | Final response, to `rsp_xbar` |
| `red_*` | out/in | — | Reduction engine client port |
| `occupancy` | out | `logic [15:0]` | Instrumentation |
| `full_stall_cnt` | out | `logic [15:0]` | Instrumentation |

**Contract.**
- For each accepted primitive, exactly one request is emitted per set member bit — no more, no
  fewer.
- Exactly one response is returned to the original requester, carrying the original tag.
- `REQ_READ_REDUCE`: the returned data is the reduction over all member responses.
- `REQ_WRITE_MCAST`: the returned status is `RSP_ERR` if any member responded `RSP_ERR`, else
  `RSP_OK`.
- The tracker never allocates beyond `MAX_OUTSTANDING_PRIM`; on full it backpressures `prim_ready`
  rather than dropping or overwriting.
- Every member response matches exactly one live tracker entry. An unmatched response is a
  detectable error, not a silent discard.
- Multiple primitives from different requesters may be in flight concurrently without
  cross-contamination of data or status.

**Parameters:** `N_PORTS`, `MAX_OUTSTANDING_PRIM`.

**Oracle:** synthetic member responses driven directly; expected fan-out set and reduced result
computed in Python. Concurrency checked by interleaving several primitives with distinct tags.
Collective semantics not needed — this is checkable one primitive at a time.

---

### 3.8 `block_engine`

**Responsibility.** Accept collective descriptors, and for each, autonomously issue member
reads, reduce, issue member writes, and write a completion status — without further requester
involvement.

| Port | Dir | Type | Notes |
| --- | --- | --- | --- |
| `clk`, `rst_n` | in | `logic` | |
| `blk_*` | in | `req_t` + v/r | `REQ_BLOCK_INVOKE` from port ingress |
| `blk_members` | in | `logic [MAX_PORTS-1:0]` | |
| `ack_*` | out | `rsp_t` + v/r | Immediate enqueue acknowledgement |
| `gen_*` | out | `req_t` + v/r | Generated member reads/writes, to `req_xbar` |
| `rsp_in_*` | in | `rsp_t` + v/r | Member responses from `rsp_xbar` |
| `red_*` | out/in | — | Reduction engine client port |
| `active_streams` | out | `logic [7:0]` | Instrumentation |
| `cbq_full_stall_cnt` | out | `logic [15:0]` | Instrumentation |

**Contract.**
- `ack` is returned **on enqueue, not on completion**, and its latency must be independent of
  `n_blocks`. This asynchrony is the block mechanism's defining advantage; a completion-coupled
  ack would silently erase the effect being measured.
- Per collective type, generated traffic is:
  - `COLL_BCAST` — read from the requester only; write to all members.
  - `COLL_REDUCE` — read from all members; write to the requester only.
  - `COLL_ALLREDUCE` — read from all members; write to all members.
- The address sequencer covers exactly `n_blocks` blocks from `in_off`/`out_off`, each exactly
  once. No gaps, no repeats.
- At most `MAX_BLOCK_STREAMS` collectives are actively generating traffic; the rest wait in a
  queue of `CBQ_DEPTH` entries per port. A full queue backpressures `blk_ready`.
- A status write to `status_off` is issued exactly once per collective, after all its member
  writes have been acknowledged, indicating success or failure.
- Any member response with `RSP_ERR` terminates that collective and is reported in its status.
- Concurrent collectives never interleave their reductions or corrupt each other's state.

**Parameters:** `N_PORTS`, `MAX_BLOCK_STREAMS`, `CBQ_DEPTH`.

**Oracle:** address-coverage check (every block touched exactly once), traffic-shape check per
collective type, ack-latency independence from `n_blocks`, and concurrency-cap assertion. The
reduction result itself is checked at integration.

---

### 3.9 `switch_top`

**Responsibility.** Instantiate `N_PORTS` ingress/egress pairs, both crossbars, both engines,
and the reduction engine; plumb parameters; expose the group-table preload path and aggregated
instrumentation counters.

**Contract.**
- Parameter changes take effect without source edits (`N_PORTS` in {8,16,32,64} all elaborate).
- Contains no logic of its own beyond instantiation and counter aggregation.
- Elaborates cleanly under `yosys -p "synth -top switch_top; stat"` at every swept `N_PORTS`.

**Oracle:** this is the level where the numpy collective oracle applies. See §5.

---

## 4. Harness module (not DUT)

### 4.1 `endpoint_model`

**Responsibility.** Behavioral accelerator model: issues requests when told to, and services
requests against a memory array with configurable latency. Instantiated in the harness,
**excluded from synthesis**.

**Contract.**
- Contains **no INC logic**. It cannot distinguish a switch-initiated member read from an
  ordinary unicast read, and must not branch on `op` beyond read-versus-write. This invariant is
  what guarantees any measured primitive-vs-block difference originates in the switch.
- Services reads and writes against its memory with `EP_RESP_LATENCY` cycles of delay.
- Returns exactly one response per request, preserving `tag` and setting `src` to its own ID.
- Exposes memory contents to the testbench for checking, and request-issue counts for
  endpoint-overhead instrumentation.

**Parameters:** `EP_RESP_LATENCY`, memory depth.

**Written in SystemVerilog, not Python**, for simulation throughput: a block collective across 32
endpoints generates thousands of beats, and a Python callback per beat would dominate sweep
runtime. Python drives it at collective granularity.

## 5. Test strategy summary

| Level | Oracle | Needs collective semantics? |
| --- | --- | --- |
| `reduction_engine` | Python sum of operands | No |
| `group_table` | Write/read-back model | No |
| `port_ingress` | Classification + rejection table | No |
| `port_egress` | Issue-count assertion, conservation | No |
| `req_xbar` / `rsp_xbar` | Tagged-packet conservation, ordering | No |
| `primitive_engine` | Expected fan-out set, reduced result | No |
| `block_engine` | Address coverage, traffic shape, ack latency | No |
| `switch_top` | **numpy collective reference model** | **Yes** |

Integration-level checks at `switch_top`, per architecture.md §12:

1. Buffer contents match the numpy reference for every collective.
2. **Mechanism equivalence** — primitive and block paths produce bit-identical results for the
   same collective and inputs.
3. **Non-member memory is bit-unchanged** — catches fan-out writing to wrong ports, a class
   functional checks alone miss.
4. Idempotence — repeating a collective with identical inputs gives identical outputs, catching
   races and nondeterminism.

The reference model and all checkers are outside the agent's edit scope. The agent may modify
module bodies; it may not modify this package, these contracts, or any checker.

## 6. Toolchain gates

Two gates run against every module.

**Verilator lint**, on the SystemVerilog source. Primary functional tool; error messages point
at real source lines.

**`sv2v` → `yosys`**, for synthesizability:

```
sv2v rtl/*.sv > build/flat.v
yosys -p "read_verilog build/flat.v; synth -top <module>; stat"
```

`sv2v` is not optional. Yosys's native SystemVerilog frontend rejects all of the following —
each verified against Yosys 0.68:

| Construct | Yosys | Verilator |
| --- | --- | --- |
| `$bits(some_type)` | rejected | fine |
| `module m import pkg::*; (...)` | rejected | fine |
| Typedef'd types in port lists — structs and simple typedefs alike | rejected | fine |
| Unpacked arrays of structs, in ports or as internal signals | rejected | fine |

These are **parser limitations, not synthesizability limitations**: all four are accepted by
Verilator and by commercial synthesis. Writing around them would mean raw `logic` vectors at
every module boundary with manual bit-slicing, which would discard the typed contract these
specs are built on and would burn agent iterations on tool trivia. Converting with `sv2v` first
lifts all four, so **modules are written in ordinary SystemVerilog** — struct ports, typed
contracts, unpacked arrays — and nobody needs to memorize Yosys's quirks.

One consequence to know: Yosys errors reference generated Verilog, not source lines. Verilator
stays the tool of record for anything functional.

`synth ... ; stat` also emits a cell count with no PDK involved, which serves as the crude area
proxy while area/Fmax proper remains deferred (architecture.md §11).

## 7. Build order

Dependency order, leaves first, so every module has a passing unit test before anything depends
on it:

1. `spec2inc_pkg`
2. `reduction_engine`, `group_table` — no dependencies, simplest oracles
3. `req_xbar`, `rsp_xbar` — independent of the engines
4. `port_ingress`, `port_egress`
5. `primitive_engine`, `block_engine`
6. `switch_top` + integration checks

**Bring-up milestone:** as soon as (3) and (4) exist, wire a minimal `switch_top` carrying
unicast traffic only, and run it end-to-end through cocotb and the CHIA node. This exercises the
harness, the Verilator flow, and the sweep plumbing well before the engines land. It is a
bring-up tactic to de-risk integration, not a design phase — the interfaces above are already
fixed against the full architecture and do not change when the engines arrive.

## 8. Open questions

Deferred to implementation, and reasonable candidates for agent exploration:

1. **Ingress-to-engine arbitration.** `N_PORTS` ingress ports feed one primitive engine and one
   block engine. Whether that arbitration lives in the engines or in a separate stage is
   unspecified above, deliberately.
2. **Egress arbitration policy** — unicast vs. primitive vs. block priority. Starving unicast
   under a large block collective is a real risk.
3. **Crossbar realization** — two physical networks, or one fabric with two virtual channels.
   Matters most at `N_PORTS=64` where wiring cost bites.
4. **Block read/write overlap** — may reads for block *k+1* issue before writes for block *k*
   retire?
5. **Reduction sharing** (`RED_SHARED`) — area saving versus throughput coupling between paths.
