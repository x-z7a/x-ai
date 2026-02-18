# Autonomous CFI (AutoGen + X-Plane MCP)

This project is a standalone autonomous CFI monitor that:

1. Monitors the full flight continuously through X-Plane MCP.
2. Uses deterministic checkride/safety rules (Private ASEL defaults).
3. Runs an AI rule-evaluator sub-agent on top of deterministic checks for additional findings.
4. Uses AutoGen-backed text generation for concise spoken coaching/debrief.
5. Speaks alerts and end-of-flight debrief through `xplm_speak_string`.
6. Runs with no task prompt, no REPL, and no user input.

## Layout

- `src/cfi_ai/main.py`: CLI entrypoint (`cfi-monitor`).
- `src/cfi_ai/runtime.py`: async orchestration workers.
- `src/cfi_ai/rules.py`: deterministic phase/rule/shutdown logic.
- `src/cfi_ai/coach.py`: AutoGen Teams orchestration (multi-expert CFI agents + chief synthesizers) and fallbacks.
- `plane_memory.json`: aircraft-specific memory profiles used by AI evaluator/coaching.
- `src/cfi_ai/cfi_tools.py`: MCP tool wrappers used by CFI runtime.
- `src/cfi_ai/xplane_mcp.py`: SSE MCP client.
- `tests/`: rule/runtime behavior tests.

## Quick Start

1. Install:

```bash
cd /Volumes/storage/git/x-ai/agentic-ais/cfi
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

2. Configure environment:

```bash
cp .env.example .env
# edit values
```

Optional: edit `plane_memory.json` to add your aircraft-specific notes (by ICAO/name).

3. Ensure X-Plane + MCP plugin are running.

4. Run autonomous monitor:

```bash
cfi-monitor
```

Optional flags:

```bash
cfi-monitor --poll-sec 0.8 --max-flight-hours 4
cfi-monitor --no-speak
```

## Defaults

- Checkride profile: Private ASEL style monitoring.
- Evaluation model: deterministic rules + AI sub-agent findings.
- Team model: round-robin teams of CFI experts (airframe, maneuvers, ACS, safety) with chief synthesis.
- Memory model: AutoGen `ListMemory` populated with detected aircraft context + profile notes, shared across team agents.
- Alert policy: coach everything (`P0`/`P1`/`P2` spoken).
- Monitoring scope: core flight phases + maneuver heuristics.
- End condition: had airborne, then on ground/stationary with engines off for hold duration.

## Safety Note

Simulation-only coaching aid. Not a substitute for a certificated instructor or real-world operational authority.
