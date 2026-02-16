# Agentic ATC (AutoGen + GitHub Copilot SDK + X-Plane MCP)

This directory contains a prototype ATC agent stack that:

1. Fetches sim state from X-Plane MCP (`xplm_dataref_get`, nav/runtime tools).
2. Consults specialist experts powered by GitHub Copilot SDK.
3. Coordinates decisions with an AutoGen controller agent.
4. Talks back through X-Plane MCP (`xplm_speak_string`).

## Layout

- `src/atc_ai/xplane_mcp.py`: SSE MCP client and tool call helpers.
- `src/atc_ai/copilot_experts.py`: phraseology/flow/safety experts using Copilot SDK.
- `src/atc_ai/atc_tools.py`: tool surface exposed to the AutoGen controller.
- `src/atc_ai/runtime.py`: AutoGen coordinator setup and execution.
- `src/atc_ai/main.py`: CLI entrypoint.

## Quick Start

1. Create a virtualenv and install:

```bash
cd agentic-ais/atc
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

2. Configure environment:

```bash
cp .env.example .env
# edit .env values
```

Required env values:

- `GITHUB_TOKEN`
- `COPILOT_MODEL`
- `COPILOT_BASE_URL`
- `AUTOGEN_MODEL`
- `AUTOGEN_BASE_URL`
- `XPLANE_MCP_SSE_URL`

Optional:

- `AUTOGEN_MODEL_FALLBACKS` (comma-separated, tried on model access failures)
- `COPILOT_USE_LOGGED_IN_USER` (`true` to use local Copilot CLI login instead of token auth)
  - In logged-in mode, `GITHUB_TOKEN` is ignored for Copilot CLI auth.
- `COPILOT_USE_CUSTOM_PROVIDER` (`false` matches `CopilotClient()` default provider behavior)

3. Ensure X-Plane is running with the MCP plugin enabled and reachable at `XPLANE_MCP_SSE_URL`.

4. Run one task:

```bash
atc-tower --task "Handle N123AB inbound to KSEA runway assignment and landing clearance."
```

5. Run interactive mode:

```bash
atc-tower --interactive
```

## Safety/Behavior Notes

- Default behavior is no automatic radio transmission unless:
  - `ATC_AUTO_TRANSMIT=true`, or
  - the controller explicitly calls `transmit_radio(..., confirm=true)`.
- This is a simulation assistant, not real-world ATC authority.

## Troubleshooting

- `ModuleNotFoundError: No module named 'copilot.models'`
  - The SDK is session-based and does not provide `copilot.models`.
  - Use this project version (which calls `CopilotClient.create_session`) and reinstall:

```bash
cd agentic-ais/atc
source .venv/bin/activate
pip install -e .
```

- `openai.PermissionDeniedError ... code: 'no_access'`
  - Your token does not have access to the configured `AUTOGEN_MODEL`.
  - Set `AUTOGEN_MODEL` to a model you can use (for example `openai/gpt-4.1-mini`).
  - You can also set `AUTOGEN_MODEL_FALLBACKS` (example: `openai/gpt-4o-mini`).

- `Session error: Authorization error, you may need to run /login`
  - Authenticate Copilot CLI: run `copilot login` (or `/login` in the Copilot CLI/TUI).
  - Or set a valid `GITHUB_TOKEN` and keep `COPILOT_USE_LOGGED_IN_USER=false`.

- `test.py works but atc-tower fails`
  - `test.py` uses default `CopilotClient()` provider flow.
  - Set `COPILOT_USE_CUSTOM_PROVIDER=false` to match that behavior.
