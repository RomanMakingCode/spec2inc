"""ChiaTools exposed to the design agent.

The agent is given exactly one capability: read and rewrite the body of the RTL
module it is working on. It cannot run tests, cannot read or edit any
testbench, the reference model, or the frozen type package, and cannot reach
any other file.

That scoping is the trust model from docs/modules.md 5, made mechanical. An
agent that could run its own evaluation could report a result that never
happened; an agent that could edit the checker could make a broken design pass.
Here the loop owns evaluation and the agent owns only the design, so a claimed
improvement is one the loop measured itself.
"""

from pathlib import Path

from chia.base.tools.ChiaTool import ChiaTool


class RtlEditTool(ChiaTool):
    """Read/write access to a single RTL file, and nothing else."""

    def setup(self, target_file: str, module_name: str):
        self.target_file = Path(target_file)
        self.module_name = module_name
        if not self.target_file.is_file():
            raise FileNotFoundError(f"no such RTL file: {self.target_file}")
        self.mcp.add_tool(self.read_rtl, name=f"{self.name}_read_rtl")
        self.mcp.add_tool(self.write_rtl, name=f"{self.name}_write_rtl")

    def read_rtl(self) -> str:
        """Return the full current text of the SystemVerilog module being worked on."""
        return self.target_file.read_text()

    def write_rtl(self, content: str) -> str:
        """Replace the SystemVerilog module with `content`.

        Pass the complete file, not a patch or a fragment. The module's port
        list and parameters must stay exactly as they are -- the surrounding
        design and its testbench are compiled against them.
        """
        # Cheap structural guard. Not a correctness check -- that is the
        # testbench's job -- just enough that a truncated or empty response is
        # rejected here rather than surfacing later as a confusing build error.
        if f"module {self.module_name}" not in content:
            return f"REJECTED: content does not define module {self.module_name}"
        if "endmodule" not in content:
            return "REJECTED: content has no endmodule"

        self.target_file.write_text(content)
        return f"wrote {len(content)} bytes, {content.count(chr(10)) + 1} lines"
