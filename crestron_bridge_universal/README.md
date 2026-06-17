# Crestron CIP Bridge (Universal)

One bridge app for all 8 stages. Per-stage behavior selected via room-category preset + per-stage fader map.

## Preset categories (v3+)

| Preset | Stages | Masking | Default projector control | Notes |
|--------|--------|---------|---------------------------|-------|
| `large`  | Stages 1, 2 | 3-axis independent | Ethernet (Barco bridge, outside this add-on) | 24 joins available per stage |
| `medium` | Stages 3, 4 | Top+Bot linked + Side | Ethernet (Panasonic via HA's PJLink integration) | 24 joins available per stage |
| `small`  | Stages 5, 6, 7, 8 | None | Crestron serial (NEC, controlled by this bridge) | Per-stage fader subsets |
| `custom` | any | None | None | Manual config (empty defaults) |

### Backward-compatible aliases

| Legacy name | Maps to |
|---|---|
| `large_theatrical` | `large` |
| `medium_theatrical` | `medium` |
| `broadcast` | `small` |

Existing add-on configs using legacy names keep working — the bridge applies the alias at startup. Fleet stages can migrate to the new names one at a time.

## Per-stage fader maps

Each preset has a `stages` dict (in `crestron_bridge.py`) keyed by `STAGE_ID`. The bridge looks up the fader map for THIS stage at startup:

```python
PRESETS["medium"]["stages"]["stage4"]["faders"]
```

Adding a new stage = adding one entry under the right preset, populated from that stage's on-stage walkthrough.

### Reserved joins (placeholder labels)

Theatrical SIMPL programs publish all 24 analog joins (10-33). Some joins drive real fixtures; others are SIMPL ghost channels (legacy fixture banks that were physically removed but the signals never cleaned up).

Per-stage maps include ALL joins, with real fixtures labeled by name (`client_center`, `pony_wall`) and unwired joins labeled `_reserved_NN`. The leading underscore is meaningful:

- Reserved joins are **SUBSCRIBED** for diagnostic visibility (visible in `/state`)
- Reserved joins are **NOT FORWARDED** to HA — `forward_state_to_ha()` filters out any label starting with `_`
- No HA input_number entity is needed for reserved joins
- When a real fixture is added in the future, just change `_reserved_25` → `new_fixture_name` and add the matching `input_number.<stage>_new_fixture_name` in HA

### Empty stages (recon pending)

Stages without a completed walkthrough have an empty `faders: {}` map. The bridge starts cleanly with 0 faders — masking + scenes still work. The add-on log warns that the stage data is missing so you know recon is needed:

```
WARNING: No stage-specific data for STAGE_ID='stage2' under preset 'large'.
Bridge will start with 0 faders (masking/scenes still functional). Add an
entry under PRESETS['large']['stages']['stage2'] when the walkthrough for
this stage completes.
```

## Configuration options

| Option | Type | Description |
|--------|------|-------------|
| `preset` | list | One of `large` / `medium` / `small` / `custom` (legacy aliases accepted) |
| `pro2_ip` | str | Pro 2 IP address |
| `ipid` | int | Pro 2 IPID |
| `stage_id` | str | Selects which entry in the preset's `stages` map to load (e.g. `stage4`) |
| `http_port` | port | Bridge HTTP API port (e.g. `8766` for Stage 1, `8765` for Stage 7) |
| `enable_projector_serial` | bool | Enable serial-driven projector endpoints + telemetry (small only) |
| `enable_masking` | bool | Enable masking direction/preset/enable endpoints (large + medium only) |
| `log_level` | list | `debug` / `info` / `warning` / `error` |

The feature flags **override** the preset's defaults if needed (e.g., a hybrid stage that has both serial projector and masking).

## Configuration examples

### Stage 1 (Large, masking, no Crestron-driven projector)
```yaml
preset: large
pro2_ip: "10.12.7.15"
ipid: 5
stage_id: "stage1"
http_port: 8766
enable_projector_serial: false
enable_masking: true
```

### Stage 4 (Medium, linked masking, Panasonic via PJLink)
```yaml
preset: medium
pro2_ip: "10.12.7.45"
ipid: 8                # IPID 8 = vestigial TPS-6X-IMCW slot, repurposed for bridge
stage_id: "stage4"
http_port: 8766
enable_projector_serial: false
enable_masking: true
```

### Stage 7 (Small, projector via Crestron serial, no masking)
```yaml
preset: small
pro2_ip: "10.12.7.75"
ipid: 4
stage_id: "stage7"
http_port: 8765
enable_projector_serial: true
enable_masking: false
```

## Endpoints

All bridges always expose: `/`, `/state`, `/fader/*`, `/scene/*`, `/all_on`, `/all_off`, `/store/*`

Conditional based on feature flags:
- **`enable_projector_serial: true`** → `/projector/enable`, `/projector/on`, `/projector/off`, `/projector/state`, `/preset/*`
- **`enable_masking: true`** → `/masking/<which>/<dir>`, `/masking/enable`, `/masking/stop`, `/masking/store/toggle`, `/masking/preset/*`

## Adding a new stage (post-recon)

After completing the on-stage walkthrough for a new stage:

1. Edit `crestron_bridge.py`
2. Locate the right preset's `stages` dict — `PRESETS["medium"]["stages"]` for theatrical stages 3/4, `PRESETS["large"]["stages"]` for stages 1/2, `PRESETS["small"]["stages"]` for broadcast stages 5/6/7/8
3. Add an entry keyed by the stage's `STAGE_ID` (e.g., `"stage3"`)
4. Populate `faders` with 24 entries (10-33) — real fixture names where wired, `_reserved_NN` for unwired joins

```python
"stages": {
    "stage3": {
        "faders": {
            10: "credenza",
            11: "patch_bay",
            # ... 13-22 mapped to real fixtures ...
            17: "_reserved_17",        # unwired
            23: "step_lights",
            24: "_reserved_24",
            25: "_reserved_25",
            # ... etc through 33 ...
        },
    },
},
```

5. Bump version in `config.yaml` (3.0.0 → 3.0.1)
6. Push to GitHub
7. HA will see the update — on Stage 3's HA VM, click Update on the add-on
8. Restart add-on. Log should show: `Preset: medium (NN faders)` with NN = count of non-reserved entries

## Migration from the old `crestron_bridge` v1 add-on (Stage 7)

The old `crestron_bridge` add-on works fine for Stage 7. To migrate to this universal version:
1. Install this `crestron_bridge_universal` add-on alongside the old one (don't uninstall yet)
2. Configure with `preset: broadcast`, same IP/IPID/port (8765)
3. Stop the old add-on
4. Start the new add-on
5. Verify Stage 7 functionality
6. Uninstall the old add-on once stable
