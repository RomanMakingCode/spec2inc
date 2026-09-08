SIM ?= verilator
TOPLEVEL_LANG ?= verilog

RTL := $(CURDIR)/../../rtl

# Swept by the CHIA sweep node; overridable here for local runs, e.g.
#   make N_PORTS=64 RED_LATENCY=1
N_PORTS ?= 16
RED_LATENCY ?= 3

VERILOG_SOURCES += $(RTL)/spec2inc_pkg.sv
VERILOG_SOURCES += $(RTL)/reduction_engine.sv

TOPLEVEL = reduction_engine
MODULE = tb_reduction_engine

# -G sets the RTL parameters; the same values reach the testbench through the
# environment so the two cannot silently disagree about what was built.
COMPILE_ARGS += -GN_PORTS=$(N_PORTS) -GRED_LATENCY=$(RED_LATENCY)
export N_PORTS
export RED_LATENCY

include $(shell cocotb-config --makefiles)/Makefile.sim
