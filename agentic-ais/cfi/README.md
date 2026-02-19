# Agentic CFI (UDP + AutoGen + X-Plane MCP)

`cfi` is a standalone CFI coaching runtime for X-Plane that:

1. Ingests aircraft state via native UDP (`RREF`) for low-latency monitoring.
2. Detects immediate hazards with deterministic rules and speaks immediately via MCP.
3. Runs a 10-agent team (9 phase experts + master CFI) every 10s over a 30s review window.
4. Uses master-led synthesis to prevent expert argument loops.
5. Uses an LLM startup bootstrap to infer an initial aircraft profile (for example ICAO) and generate a welcome message.

## Flight Phases

- `preflight`
- `taxi_out`
- `takeoff`
- `initial_climb`
- `cruise`
- `descent`
- `approach`
- `landing`
- `taxi_in`

## Quick Start

```bash
cd /Volumes/storage/git/x-ai/agentic-ais/cfi
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# edit .env
```

Run continuous mode:

```bash
cfi-coach
```

Run bounded test mode:

```bash
cfi-coach --duration-sec 120 --dry-run
```

## X-Plane Discovery

- By default, CFI discovers X-Plane via UDP multicast `BECN` beacons.
- Default beacon settings:
  - `XPLANE_DISCOVERY_ENABLED=true`
  - `XPLANE_BEACON_MULTICAST_GROUP=239.255.1.1`
  - `XPLANE_BEACON_PORT=49707`
- If discovery is unavailable in your network, set explicit:
  - `XPLANE_UDP_HOST=<xplane-ip>`
  - `XPLANE_UDP_PORT=49000`
  - `XPLANE_DISCOVERY_ENABLED=false`

## CLI Flags

- `--duration-sec <int>` run for a bounded duration
- `--no-nonurgent-speak` mute non-urgent team speech
- `--dry-run` skip MCP speech calls but keep decisions/logging

## Logs

Always-on JSONL logs:

- `team.chat.log.jsonl`
- `runtime.events.log.jsonl`

Detailed telemetry is implemented but disabled by default (`CFI_TELEMETRY_ENABLED=false`).

## Startup Behavior

- On startup, CFI waits briefly for initial UDP telemetry, asks an LLM to infer session aircraft parameters (including ICAO), and stores that profile for later coaching decisions.
- Startup LLM output also includes a plane-specific hazard profile (enabled rules + thresholds), which is applied immediately to deterministic hazard monitoring.
- Startup LLM output can include per-hazard speech variants so urgent callouts are less repetitive.
- The same startup LLM pass generates a welcome message that can be spoken through MCP.
- If bootstrap parsing fails, CFI falls back to a default `C172` profile and continues.

## Shutdown Debrief

- CFI auto-detects engine shutdown from UDP telemetry (engine state/RPM when available, with parked-ground fallback logic).
- When shutdown is detected after a completed flight, CFI triggers a one-time full-flight debrief immediately and writes `engine_shutdown_detected` + `shutdown_debrief` events to `runtime.events.log.jsonl`.
- In continuous daemon mode, post-shutdown flight activity (engine restart/taxi/takeoff) starts a fresh flight cycle so additional flights also get their own shutdown debriefs.
- If shutdown is never detected, runtime still runs one final debrief on process exit as fallback.
- If debrief speech is available and not suppressed by a recent urgent alert, CFI can speak final feedback.
- `CFI_SHUTDOWN_DETECT_DWELL_SEC` tunes how long shutdown conditions must hold before triggering debrief.

## Hazard Speech Variants

- A runtime background agent can refresh hazard phrase variants without blocking urgent monitoring.
- Urgent alerts always remain deterministic; phrase generation is asynchronous and cached.
- `CFI_HAZARD_PHRASE_RUNTIME_ENABLED` toggles runtime refresh.
- `CFI_HAZARD_PHRASE_REFRESH_SEC` sets refresh cadence.

## Taxi Speed False-Positive Suppression

- Taxi speed monitoring is suppressed during takeoff ground roll acceleration.
- Taxi-in warnings are suppressed during high-speed landing rollout until speed decays into true taxi regime.

## Retry Behavior

- If X-Plane MCP or initial connectivity is unavailable, CFI retries startup with backoff.
- `XPLANE_RETRY_SEC` controls retry delay.
- `XPLANE_START_MAX_RETRIES=0` means retry indefinitely.

## Safety Notes

- Immediate hazard speech is deterministic and does not wait for LLM output.
- High-risk non-urgent review findings can be promoted to a priority spoken coaching channel if normal non-urgent cadence would otherwise suppress them.
- MCP command execution is implemented as an interface but disabled in v1.
- This is a simulator assistant, not real-world flight instruction authority.
