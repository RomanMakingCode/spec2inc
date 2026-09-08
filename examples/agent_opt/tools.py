"""ChiaTools exposed to the design agent.

The agent is given exactly one capability: read and rewrite the body of the RTL
module under optimization. It cannot run tests, cannot read or edit the
testbench, the reference model, or the frozen type package, and cannot reach any
other file.

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

    def setup(self, target_file: str):
        self.target_file = Path(target_file)
        if not self.target_file.is_file():
            raise FileNotFoundError(f"no such RTL file: {self.target_file}")
        self.mcp.add_tool(self.read_rtl, name=f"{self.name}_read_rtl")
        self.mcp.add_tool(self.write_rtl, name=f"{self.name}_write_rtl")

    def read_rtl(self) -> str:
        """Return the full current text of the SystemVerilog module being optimized."""
        return self.target_file.read_text()

    def write_rtl(self, content: str) -> str:
        """Replace the SystemVerilog module with `content`.

        Pass the complete file, not a patch or a fragment. The module's port
        list and parameters must stay exactly as they are -- the surrounding
        design and its testbench are compiled against them.
        """
        if "module reduction_engine" not in content:
            return "REJECTED: content does not define module reduction_engine"
        if "endmodule" not in content:
            return "REJECTED: content has no endmodule"

        self.target_file.write_text(content)
        return f"wrote {len(content)} bytes, {content.count(chr(10)) + 1} lines"
