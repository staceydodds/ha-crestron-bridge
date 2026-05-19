# Crestron CIP Bridge — Home Assistant Add-on

A long-running HTTP-to-CIP bridge that lets Home Assistant control a Crestron Pro 2 (and similar Crestron systems) as part of the AV control modernization project.

One add-on instance per stage. Each instance is configured for a specific Pro 2 IP, IPID, and stage entity prefix.

## Features

- **Persistent CIP connection** — maintains a permanent connection to one Pro 2 over CIP/TCP port 41794
- **Translates HTTP to CIP** — HA's `rest_command` entries POST to this add-on; it dispatches the corresponding CIP commands
- **State telemetry** — captures Pro 2 state changes (fader values, lamp hours, projector input, warming/cooling progress) and exposes them via `GET /state`
- **Forwards state to HA** — pushes fader changes back to HA's `input_number` entities so the dashboard stays in sync with the physical hardware
- **Auto-restart** — supervisor automatically restarts the add-on if it crashes
- **Per-stage configuration** — entity names are templated from `stage_id` config, so one add-on codebase serves all 8 stages
- **No long-lived tokens** — uses the supervisor token automatically; nothing sensitive in the source

## Installation (Local Add-on)

1. Copy the entire `addon/` folder to your HA box at `/addons/crestron_bridge/`. On HAOS, this is typically done via the SSH add-on (`scp` or `cp`) or via the File Editor add-on.

2. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ (top right) → Reload**.

3. Scroll down to the **"Local add-ons"** section. You should see **"Crestron CIP Bridge"**.

4. Click it, then click **Install**. First install takes a few minutes (Docker has to pull the base image and install Python + cipclient).

5. After install, go to the **Configuration** tab and set:
   - `pro2_ip`: IP address of this stage's Pro 2 (e.g., `192.168.1.70` for Stage 7)
   - `ipid`: Crestron IPID slot the bridge should register as (almost always `4` — the XPanel slot)
   - `stage_id`: short string used as entity prefix (e.g., `stage7`). The bridge maps fader joins to `input_number.<stage_id>_work_rear` etc.
   - `http_port`: HTTP port the bridge listens on (default `8765`)
   - `log_level`: `info` for normal use, `debug` to see raw CIP messages

6. On the **Info** tab, toggle **"Start on boot"** ON. Click **Start**.

7. Watch the **Log** tab. You should see:
   ```
   Starting Crestron Bridge
   Connecting to Pro 2 at <IP> as IPID 0x04...
   Registered IPID 0x04
   Pre-registered: projector gauge joins 7 (warming) and 8 (cooling)
   Pre-registered: 50 serial joins in range 1-50
   Connected. HTTP server listening on http://0.0.0.0:8765
   ```

8. Stop the old standalone `python3 crestron_bridge.py` if it's still running in an SSH terminal.

## How HA Talks to the Add-on

HA's `rest_command` entries in `configuration.yaml` use the URL `http://<HA_BOX_IP>:8765/...`. The add-on exposes port 8765 on the host network (configured in `config.yaml`), so HA can reach it the same way it reached the standalone bridge — no config changes needed in `configuration.yaml`.

For state polling, HA's `rest:` sensor block hits `http://<HA_BOX_IP>:8765/state` every 15 seconds.

## How the Add-on Talks Back to HA

Fader state forwarding uses the supervisor proxy: `http://supervisor/core/api/services/input_number/set_value`. The supervisor automatically injects a `SUPERVISOR_TOKEN` env var into the add-on container, which is used for auth. No long-lived access token needed in source code.

This requires `homeassistant_api: true` in `config.yaml` (already set).

## Multi-Stage Rollout

For each additional stage (5, 6, 8):

1. Either install this add-on multiple times (one per stage) — currently HA add-ons are usually single-instance, so you'd publish copies with different slugs (`crestron_bridge_stage5`, etc.)
2. Or refactor the add-on to support multiple stages in one instance (future improvement)

For now, the single-instance design is simplest. Each Pro 2 needs its own bridge process anyway, so one container per stage matches the natural architecture.

## Troubleshooting

**Add-on won't start, log shows "Failed to connect to Pro 2"**
- Verify `pro2_ip` is correct
- Verify the Pro 2 is on the network and reachable: from SSH terminal, `ping <pro2_ip>` and `nc -vz <pro2_ip> 41794`
- Check that no other client is occupying the IPID slot — the Pro 2 will refuse a connection if the IPID is already taken

**Fader changes don't appear in HA dashboard**
- Verify `stage_id` matches the prefix on your `input_number` entities
- Check the add-on log for `Failed to forward state to HA` warnings — these indicate the supervisor token isn't working
- Confirm `homeassistant_api: true` is in `config.yaml`

**HA's REST sensor shows "unavailable"**
- Verify port `8765` is mapped in the add-on configuration tab (host port should be `8765`)
- Try `curl http://<HA_BOX_IP>:8765/state` from another machine — should return JSON
- Restart HA after first add-on install (sometimes needed for HA to discover the new REST endpoint)

## Files

- `config.yaml` — add-on manifest, options schema, port mappings
- `Dockerfile` — Alpine base + Python + cipclient
- `run.sh` — startup wrapper, reads HA options into env vars
- `crestron_bridge.py` — the bridge service itself
- `README.md` — this file
