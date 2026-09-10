SIM ?= verilator
TOPLEVEL_LANG ?= verilog

# Overridable so the same testbench can be pointed at a candidate
# implementation without touching the repo's own rtl/.
RTL ?= $(CURDIR)/../../rtl

N_PORTS ?= 8

VERILOG_SOURCES += $(RTL)/spec2inc_pkg.sv
VERILOG_SOURCES += $(RTL)/req_xbar.sv

TOPLEVEL = req_xbar
MODULE = tb_req_xbar

COMPILE_ARGS += -GN_PORTS=$(N_PORTS)
export N_PORTS

include $(shell cocotb-config --makefiles)/Makefile.sim
