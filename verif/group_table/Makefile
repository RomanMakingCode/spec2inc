SIM ?= verilator
TOPLEVEL_LANG ?= verilog

# Overridable so the same testbench can be pointed at a candidate
# implementation without touching the repo's own rtl/.
RTL ?= $(CURDIR)/../../rtl

N_PORTS ?= 16
GROUP_TABLE_ENTRIES ?= 64

VERILOG_SOURCES += $(RTL)/spec2inc_pkg.sv
VERILOG_SOURCES += $(RTL)/group_table.sv

TOPLEVEL = group_table
MODULE = tb_group_table

COMPILE_ARGS += -GN_PORTS=$(N_PORTS) -GGROUP_TABLE_ENTRIES=$(GROUP_TABLE_ENTRIES)
export N_PORTS
export GROUP_TABLE_ENTRIES

include $(shell cocotb-config --makefiles)/Makefile.sim
