"""Smallest possible agentic CHIA loop: one Gemini call dispatched through Ray.

This does no useful design work. It exists to retire the risks that would
otherwise surface late -- that Vertex credentials reach a CHIA worker, that an
LLM node dispatches and returns, and what a single call costs in wall time and
tokens. Everything agentic in this project sits on top of those three facts.

    export THIS_MACHINE=$(hostname -I | awk '{print $1}')
    export GOOGLE_CLOUD_PROJECT=<project id>
    chia up -y examples/agent_smoke/cluster.yaml
    python examples/agent_smoke/loop.py
    chia down -y examples/agent_smoke/cluster.yaml

Model and location are environment-overridable:

    GEMINI_MODEL=gemini-3.1-pro-preview python examples/agent_smoke/loop.py
"""

import os
import time

from chia.base.ChiaFunction import get
from chia.models.vertex import VertexGeminiLLM

# CHIA's VertexGeminiLLM defaults to us-central1, where gemini-3.1-pro-preview
# returns 404 -- the Pro models are served from the global endpoint. Default to
# global so switching models does not silently break.
#
# Measured on this project, round trip through a CHIA node, global endpoint:
#   gemini-2.5-flash          ~3s     404 in us-central1? no, available in both
#   gemini-3.1-pro-preview    ~2s     global only
#   gemini-2.5-pro            ~16-20s global only
# gemini-3.1-pro-preview is both the fastest and the strongest, so it is the
# one to reach for when real design work starts.
#
# Known hazard: token refreshes against oauth2.googleapis.com intermittently
# hang on this host (~1 call in 3-4), and google-auth's read timeout is 120s,
# so a single unlucky refresh can stall an iteration for minutes. The same
# host also intermittently stalls TLS to github. If this becomes painful in a
# long loop, the fix is to reuse one client across calls rather than
# re-authenticating per prompt.
DEFAULT_LOCATION = "global"
DEFAULT_MODEL = "gemini-2.5-flash"

PROMPT = "Reply with exactly the single word: PONG"


def main() -> None:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("GOOGLE_CLOUD_PROJECT is not set")

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    location = os.environ.get("GEMINI_LOCATION", DEFAULT_LOCATION)

    llm = VertexGeminiLLM(
        model=model,
        project=project,
        location=location,
        system_message="You are terse. Answer with exactly what is asked.",
        retries=2,
    )

    print(f"model={model} location={location} project={project}")

    started = time.time()
    result = get(llm.prompt.chia_remote(llm, PROMPT))
    elapsed = time.time() - started

    print(f"success={result.success} returncode={result.returncode} elapsed={elapsed:.1f}s")
    print(f"response={result.result.strip()!r}")

    assert result.success, f"Vertex call failed: {result.stderr}"
    assert "PONG" in result.result.upper(), f"unexpected response: {result.result!r}"
    print("AGENT SMOKE TEST OK: Gemini reachable from a CHIA node.")


if __name__ == "__main__":
    main()
