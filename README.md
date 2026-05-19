# Home Assistant Add-ons for Crestron AV Control

Custom HA add-ons supporting the AV Control Modernization project — replacing discontinued Crestron iPad apps with Home Assistant dashboards for post-production sound mixing stages.

## Add-ons in this repository

### [Crestron CIP Bridge](./crestron_bridge)

A long-running HTTP-to-CIP bridge that exposes a Crestron Pro 2 (or similar Crestron system) as a REST API for Home Assistant. One add-on instance per stage. Captures live telemetry (fader values, lamp hours, projector warming/cooling state) and translates HA's `rest_command` calls into CIP commands on a persistent connection.

See [crestron_bridge/README.md](./crestron_bridge/README.md) for full documentation.

## Installation

In Home Assistant:

1. Settings → Apps → ⋮ (top right) → **Repositories**
2. Paste this repository's URL: `https://github.com/staceydodds/ha-crestron-bridge`
3. Click Add
4. The add-on(s) will appear in the Apps store. Click into one to install.

Or via CLI:

```bash
ha store add https://github.com/staceydodds/ha-crestron-bridge
```

After the repo is added, install the add-on:

```bash
ha store apps install crestron_bridge
```

## Project context

- 8 mixing stages at a post-production sound facility
- Stages 5–8 are "broadcast" rooms (7 lighting channels, 8 scene presets, projector control)
- Stages 1–2 are Atmos rooms (24 lighting channels)
- Existing Crestron Pro 2 controllers stay in place; HA becomes the new touch-panel layer
- One bridge add-on instance per stage, configured per stage's Pro 2 IP and entity prefix

## Maintainer

Stacey Dodds — `staceydodds@outlook.com`
