// group_table -- per-port group membership storage. Spec: docs/modules.md 3.2.
//
// One instance lives inside each port_ingress, holding the accelerator bitmask
// for every group that port can address. Reads are combinational because
// port_ingress resolves a request's group in the cycle it classifies it;
// writes are synchronous, preloaded by privileged software in the real system.
//
// The index space is ID_W bits wide while the table may be narrower, so an
// index is range-checked at full width and narrowed only to address the array.

module group_table
    import spec2inc_pkg::*;
#(
    parameter int N_PORTS             = 16,
    parameter int GROUP_TABLE_ENTRIES = 64
) (
    input  logic            clk,
    input  logic            rst_n,

    // Synchronous write port. Preloaded by privileged software in the real
    // system; driven by the testbench here.
    input  logic            wr_en,
    input  logic [ID_W-1:0] wr_idx,
    input  logic            wr_valid_bit,
    input  port_mask_t      wr_members,

    // Combinational read port: both outputs reflect rd_idx in the same cycle.
    input  logic [ID_W-1:0] rd_idx,
    output logic            rd_valid_bit,
    output port_mask_t      rd_members
);

    localparam int AW = $clog2(GROUP_TABLE_ENTRIES);
    localparam int IDX_W = (AW > 0) ? AW : 1;
    
    typedef logic [IDX_W-1:0] idx_t;

    idx_t w_addr;
    idx_t r_addr;

    // Explicit cast silences Verilator WIDTHTRUNC warnings when ID_W > IDX_W
    // (e.g. 6-bit wr_idx indexing a 16-entry array, which needs 4 bits)
    assign w_addr = idx_t'(wr_idx);
    assign r_addr = idx_t'(rd_idx);

    logic [GROUP_TABLE_ENTRIES-1:0] valid_array;
    port_mask_t mem_array [GROUP_TABLE_ENTRIES];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_array <= '0;
        end else begin
            if (wr_en && (int'(wr_idx) < GROUP_TABLE_ENTRIES)) begin
                valid_array[w_addr] <= wr_valid_bit;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (wr_en && (int'(wr_idx) < GROUP_TABLE_ENTRIES)) begin
            mem_array[w_addr] <= wr_members;
        end
    end

    always_comb begin
        if (int'(rd_idx) < GROUP_TABLE_ENTRIES) begin
            rd_valid_bit = valid_array[r_addr];
            rd_members   = mem_array[r_addr];
        end else begin
            rd_valid_bit = 1'b0;
            rd_members   = '0;
        end
    end

endmodule
