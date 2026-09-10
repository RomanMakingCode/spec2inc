// req_xbar -- request interconnect. Spec: docs/modules.md 3.5.
//
// INTERFACE ONLY. The body is deliberately absent: this is the starting point
// handed to the design agent in an implement-from-spec run, so that the port
// list is fixed by the loop rather than invented by the agent. It compiles and
// fails every test.
//
// REPLACE THIS HEADER when implementing: describing the finished module as an
// empty interface is worse than no comment at all.

module req_xbar
    import spec2inc_pkg::*;
#(
    parameter int N_PORTS = 16
) (
    input  logic clk,
    input  logic rst_n,

    // Sources, flattened: source s occupies [s*REQ_W +: REQ_W]. Indices 0 to
    // N_PORTS-1 are the per-port unicast streams from port_ingress; index
    // N_PORTS is the primitive engine and N_PORTS+1 the block engine.
    input  logic [(N_PORTS+2)*REQ_W-1:0] s_req,
    input  logic [N_PORTS+1:0]           s_valid,
    output logic [N_PORTS+1:0]           s_ready,

    // Sinks, flattened the same way: one per port egress, selected by the
    // dst field of the beat being routed.
    output logic [N_PORTS*REQ_W-1:0]     m_req,
    output logic [N_PORTS-1:0]           m_valid,
    input  logic [N_PORTS-1:0]           m_ready
);

    // TODO: implement per docs/modules.md 3.5.
    assign s_ready = '0;
    assign m_req   = '0;
    assign m_valid = '0;

endmodule
