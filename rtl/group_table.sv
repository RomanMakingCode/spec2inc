// group_table -- per-port group membership storage. Spec: docs/modules.md 3.2.
//
// INTERFACE ONLY. The body is deliberately absent: this is the starting point
// handed to the design agent in an implement-from-spec run, so that the port
// list is fixed by the loop rather than invented by the agent. It compiles and
// fails every test.

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

    logic [GROUP_TABLE_ENTRIES-1:0] valid_array;
    port_mask_t mem_array [GROUP_TABLE_ENTRIES];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            valid_array <= '0;
        end else begin
            if (wr_en && (int'(wr_idx) < GROUP_TABLE_ENTRIES)) begin
                valid_array[wr_idx] <= wr_valid_bit;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (wr_en && (int'(wr_idx) < GROUP_TABLE_ENTRIES)) begin
            mem_array[wr_idx] <= wr_members;
        end
    end

    always_comb begin
        if (int'(rd_idx) < GROUP_TABLE_ENTRIES) begin
            rd_valid_bit = valid_array[rd_idx];
            rd_members   = mem_array[rd_idx];
        end else begin
            rd_valid_bit = 1'b0;
            rd_members   = '0;
        end
    end

endmodule
