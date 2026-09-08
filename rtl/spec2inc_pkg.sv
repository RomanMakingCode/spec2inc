// spec2inc_pkg -- shared type and constant contract for the INC switch.
//
// FROZEN CONTRACT. This file is the read-only architectural contract referenced
// by docs/modules.md. Design and verification agents may edit module bodies and
// testbenches; they may not edit this file. Changing a type here changes the
// meaning of every module's spec at once, so edits belong to a deliberate
// architecture revision, not to an optimization pass.
//
// Widths are package localparams rather than module parameters because packed
// struct fields need elaboration-time constants, and parameterized types are the
// SystemVerilog that yosys handles worst. N_PORTS stays a module parameter and is
// swept; port masks are always MAX_PORTS wide with only the low N_PORTS bits
// meaningful.

package spec2inc_pkg;

    // ---------------------------------------------------------------- sizing

    localparam int MAX_PORTS  = 64;               // upper bound on radix
    localparam int DATA_W     = 256;              // bits per beat
    localparam int ADDR_W     = 48;
    localparam int TAG_W      = 8;
    localparam int ID_W       = $clog2(MAX_PORTS);
    localparam int LEN_W      = 16;               // transfer length in beats
    localparam int RED_LANE_W = 32;               // reduction lane width
    localparam int RED_LANES  = DATA_W / RED_LANE_W;

    typedef logic [MAX_PORTS-1:0] port_mask_t;

    // --------------------------------------------------------------- request

    // REQ_READ and REQ_WRITE are used both for endpoint-initiated unicast and
    // for switch-initiated member access. An endpoint cannot tell the two apart,
    // which is what guarantees a measured primitive-vs-block difference
    // originates in the switch rather than in endpoint-side special-casing.
    typedef enum logic [2:0] {
        REQ_READ         = 3'd0,
        REQ_WRITE        = 3'd1,
        REQ_READ_REDUCE  = 3'd2,   // collective primitive
        REQ_WRITE_MCAST  = 3'd3,   // collective primitive
        REQ_BLOCK_INVOKE = 3'd4    // block collective descriptor delivery
    } req_op_e;

    // Header fields ride on every beat rather than occupying a separate address
    // channel. Recorded simplification (docs/modules.md 2.2): it keeps each
    // interconnect a single network, and both mechanisms pay it equally.
    typedef struct packed {
        req_op_e           op;
        logic [ID_W-1:0]   src;    // requesting endpoint; stamped by port_ingress
        logic [ID_W-1:0]   dst;    // endpoint ID, or group ID when op is a primitive
        logic [TAG_W-1:0]  tag;
        logic [ADDR_W-1:0] addr;
        logic [LEN_W-1:0]  len;
        logic [DATA_W-1:0] data;
        logic              last;
    } req_t;

    // -------------------------------------------------------------- response

    typedef enum logic [1:0] {
        RSP_OK  = 2'd0,
        RSP_ERR = 2'd1
    } rsp_status_e;

    // Selects the response crossbar's sink. Engines consume member responses;
    // endpoints consume final responses.
    typedef enum logic [1:0] {
        DEST_ENDPOINT  = 2'd0,
        DEST_PRIMITIVE = 2'd1,
        DEST_BLOCK     = 2'd2
    } rsp_dest_e;

    typedef struct packed {
        rsp_dest_e         dest_class;
        logic [ID_W-1:0]   dst;    // endpoint index when dest_class == DEST_ENDPOINT
        logic [ID_W-1:0]   src;    // responding endpoint
        logic [TAG_W-1:0]  tag;
        rsp_status_e       status;
        logic [DATA_W-1:0] data;
        logic              last;
    } rsp_t;

    // ------------------------------------------------------ block collectives

    typedef enum logic [1:0] {
        COLL_BCAST     = 2'd0,     // read root, write all members
        COLL_REDUCE    = 2'd1,     // read all members, write root
        COLL_ALLREDUCE = 2'd2      // read all members, write all members
    } coll_type_e;

    // Delivered in the data payload of a REQ_BLOCK_INVOKE beat.
    typedef struct packed {
        coll_type_e        coll;
        logic [ID_W-1:0]   group;
        logic [ADDR_W-1:0] in_off;
        logic [ADDR_W-1:0] out_off;
        logic [ADDR_W-1:0] status_off;
        logic [LEN_W-1:0]  n_blocks;
    } block_desc_t;

    localparam int BLOCK_DESC_W = $bits(block_desc_t);   // 168, fits one beat

    // Written by the block engine to status_off exactly once per collective,
    // after all its member writes have been acknowledged. The requesting
    // endpoint learns of completion by polling this; it is never told directly.
    typedef struct packed {
        logic done;
        logic error;
    } block_status_t;

endpackage
