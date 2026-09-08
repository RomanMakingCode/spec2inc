// reduction_engine -- lane-wise integer reduction, shared by the primitive and
// block INC engines. Spec: docs/modules.md 3.1.
//
// Combines up to N_PORTS operand beats into one, adding RED_LANE_W-bit lanes
// independently and wrapping on overflow. Because that operation is associative
// and commutative, the result does not depend on which slots the operands
// occupy -- the property that lets the block engine collect member reads in
// whatever order they arrive.

module reduction_engine
    import spec2inc_pkg::*;
#(
    parameter int N_PORTS     = 16,
    parameter int RED_LATENCY = 3    // pipeline stages; must be >= 1
) (
    input  logic                         clk,
    input  logic                         rst_n,

    // Operand set. op_data is a flat vector rather than a packed 2-D array so
    // that its layout is unambiguous to verilator, sv2v, and the cocotb
    // testbench alike: operand p occupies bits [p*DATA_W +: DATA_W], and lane l
    // within a beat occupies [l*RED_LANE_W +: RED_LANE_W].
    input  logic                         op_valid,
    output logic                         op_ready,
    input  logic [N_PORTS-1:0]           op_mask,
    input  logic [N_PORTS*DATA_W-1:0]    op_data,
    input  logic [TAG_W-1:0]             op_id,

    // Reduced result. op_id is carried through untouched so the client can match
    // a result to the transaction that produced it.
    output logic                         res_valid,
    input  logic                         res_ready,
    output logic [DATA_W-1:0]            res_data,
    output logic [TAG_W-1:0]             res_id
);

    // ------------------------------------------------------------ reduction

    // Accumulates per lane, so a carry out of lane l is discarded rather than
    // bleeding into lane l+1. An unset mask bit contributes nothing, which makes
    // a single-bit mask a pass-through and an all-zero mask produce zero. The
    // all-zero case is illegal per the spec; the engine does not police it.
    logic [DATA_W-1:0] sum_c;

    always_comb begin
        sum_c = '0;
        for (int p = 0; p < N_PORTS; p++) begin
            if (op_mask[p]) begin
                for (int l = 0; l < RED_LANES; l++) begin
                    sum_c[l*RED_LANE_W +: RED_LANE_W] =
                          sum_c[l*RED_LANE_W +: RED_LANE_W]
                        + op_data[p*DATA_W + l*RED_LANE_W +: RED_LANE_W];
                end
            end
        end
    end

    // ------------------------------------------------------------- pipeline

    // The whole pipeline freezes while a finished result is waiting to be
    // accepted, so nothing is dropped and no skid buffer is needed. Latency is
    // therefore exactly RED_LATENCY cycles of forward progress: exactly
    // RED_LATENCY clocks when the consumer keeps res_ready high, and longer by
    // however many cycles it backpressures.
    logic stall;

    logic [RED_LATENCY-1:0] vld_q;
    logic [DATA_W-1:0]      data_q [RED_LATENCY];
    logic [TAG_W-1:0]       id_q   [RED_LATENCY];

    assign stall    = res_valid && !res_ready;
    assign op_ready = !stall;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            vld_q <= '0;
            for (int s = 0; s < RED_LATENCY; s++) begin
                data_q[s] <= '0;
                id_q[s]   <= '0;
            end
        end else if (!stall) begin
            // op_ready is high whenever !stall, so op_valid alone decides
            // whether this cycle admits a new operand set.
            vld_q[0]  <= op_valid;
            data_q[0] <= sum_c;
            id_q[0]   <= op_id;
            for (int s = 1; s < RED_LATENCY; s++) begin
                vld_q[s]  <= vld_q[s-1];
                data_q[s] <= data_q[s-1];
                id_q[s]   <= id_q[s-1];
            end
        end
    end

    assign res_valid = vld_q[RED_LATENCY-1];
    assign res_data  = data_q[RED_LATENCY-1];
    assign res_id    = id_q[RED_LATENCY-1];

    // Note for later optimization: the adder chain above is entirely
    // combinational, so RED_LATENCY currently buys latency without buying
    // frequency -- at N_PORTS=64 the critical path runs through 64 adds. The
    // intended improvement is to distribute a balanced adder tree across the
    // RED_LATENCY registers. Left deliberately unoptimized: it is a
    // self-contained, measurable target with an exact oracle, which makes it a
    // good first task for the design agent.

endmodule
