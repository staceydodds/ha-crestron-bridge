# Crestron CIP Bridge (Universal)

One bridge app for all 8 stages. Per-stage behavior selected via preset + feature flags.

## Preset categories

| Preset | Stages | Faders | Default features |
|--------|--------|--------|------------------|
| `large_theatrical` | Stages 1, 2 | 24 (joins 10-33) | masking ON, projector serial OFF |
| `medium_theatrical` | Stages 3, 4 | TBD — populate after Stage 3/4 walkthrough | masking ON, projector serial OFF |
| `broadcast` | Stages 5, 6, 7, 8 | 7 (joins 11-18) | projector serial ON, masking OFF |
| `custom` | any | none (manual config) | both OFF |

**Note:** "projector serial" means the projector is controlled via the Crestron Pro 2's serial port (the broadcast stages use NEC projectors connected to the Crestron). Theatrical stages 1-4 also have projectors (Barco), but those are controlled separately via web UI, not through the Crestron — hence `enable_projector_serial: false` for theatrical presets.

## Configuration options

| Option | Type | Description |
|--------|------|-------------|
| `preset` | list | One of `large_theatrical` / `medium_theatrical` / `broadcast` / `custom` |
| `pro2_ip` | str | Pro 2 IP address |
| `ipid` | int | Pro 2 IPID |
| `stage_id` | str | Used to template HA entity names (`input_number.{stage_id}_<fader>`) |
| `http_port` | port | Bridge HTTP API port (e.g. `8766` for Stage 1, `8765` for Stage 7) |
| `enable_projector_serial` | bool | Enable serial-driven projector endpoints + telemetry (broadcast only) |
| `enable_masking` | bool | Enable masking direction/preset/enable endpoints (theatrical only) |
| `log_level` | list | `debug` / `info` / `warning` / `error` |

The feature flags **override** the preset's defaults if needed (e.g., a hybrid stage that has both serial projector and masking).

## Configuration examples

### Stage 1 (Large Theatrical, masking, no Crestron-driven projector)
```yaml
preset: large_theatrical
pro2_ip: "10.12.7.15"
ipid: 5
stage_id: "stage1"
http_port: 8766
enable_projector_serial: false
enable_masking: true
```

### Stage 2 (same preset, different Pro 2)
```yaml
preset: large_theatrical
pro2_ip: "10.12.7.25"
ipid: 5
stage_id: "stage2"
http_port: 8766
enable_projector_serial: false
enable_masking: true
```

### Stage 3 / 4 (Medium Theatrical — once fader list is populated in code)
```yaml
preset: medium_theatrical
pro2_ip: "10.12.7.35"   # or .45 for Stage 4
ipid: 5
stage_id: "stage3"
http_port: 8766
enable_projector_serial: false
enable_masking: true
```

### Stage 7 (Broadcast, projector via Crestron serial, no masking)
```yaml
preset: broadcast
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

## Populating Medium Theatrical (Stages 3/4)

Edit `crestron_bridge.py`, find `PRESETS["medium_theatrical"]["faders"]`, and add the join → name mappings after walking through each stage's physical lights:

```python
"medium_theatrical": {
    "faders": {
        10: "fader_name_a",
        11: "fader_name_b",
        # ... add each confirmed join
    },
    "default_features": {"projector_serial": False, "masking": True},
},
```

Bump version in `config.yaml`, push to GitHub, HA will see the update.

## Migration from the old `crestron_bridge` v1 add-on (Stage 7)

The old `crestron_bridge` add-on works fine for Stage 7. To migrate to this universal version:
1. Install this `crestron_bridge_universal` add-on alongside the old one (don't uninstall yet)
2. Configure with `preset: broadcast`, same IP/IPID/port (8765)
3. Stop the old add-on
4. Start the new add-on
5. Verify Stage 7 functionality
6. Uninstall the old add-on once stable
