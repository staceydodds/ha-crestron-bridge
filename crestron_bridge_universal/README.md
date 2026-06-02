# Crestron CIP Bridge (Universal)

One bridge app for all 8 stages. Per-stage behavior selected via preset + feature flags.

## Configuration options

| Option | Type | Description |
|--------|------|-------------|
| `preset` | list | `stage1` (24 faders + masking), `stage7` (7 faders + projector), or `custom` |
| `pro2_ip` | str | Pro 2 IP address (e.g. `10.12.7.15` for Stage 1, `10.12.7.75` for Stage 7) |
| `ipid` | int | Pro 2 IPID (e.g. `5` for Stage 1, `4` for Stage 7) |
| `stage_id` | str | Used to template HA entity names (`input_number.{stage_id}_<fader>`) |
| `http_port` | port | Bridge HTTP API port (e.g. `8766` for Stage 1, `8765` for Stage 7) |
| `enable_projector` | bool | Enable projector control + telemetry endpoints |
| `enable_masking` | bool | Enable masking control + indicator endpoints |
| `log_level` | list | `debug` / `info` / `warning` / `error` |

When you change the `preset`, the bridge auto-loads the corresponding fader map. The `enable_projector` and `enable_masking` flags override the preset's defaults if you need a hybrid stage.

## Configuration examples

### Stage 1 (24 faders + masking, no projector)
```yaml
preset: stage1
pro2_ip: "10.12.7.15"
ipid: 5
stage_id: "stage1"
http_port: 8766
enable_projector: false
enable_masking: true
```

### Stage 7 (7 faders + projector, no masking)
```yaml
preset: stage7
pro2_ip: "10.12.7.75"
ipid: 4
stage_id: "stage7"
http_port: 8765
enable_projector: true
enable_masking: false
```

## Endpoints

All bridges always expose: `/`, `/state`, `/fader/*`, `/scene/*`, `/all_on`, `/all_off`, `/store/*`

Conditional based on feature flags:
- **Projector** routes: `/projector/enable`, `/projector/on`, `/projector/off`, `/projector/state`, `/preset/*`
- **Masking** routes: `/masking/<which>/<dir>`, `/masking/enable`, `/masking/stop`, `/masking/store/toggle`, `/masking/preset/*`

## Adding a new stage

When stages 2-6 / 8 come online, add a new entry to the `PRESETS` dict in `crestron_bridge.py`:
```python
"stage2": {
    "faders": {
        # join: short_name
        10: "fader_name_a",
        11: "fader_name_b",
        ...
    },
    "default_features": {"projector": False, "masking": True},
},
```
Then bump version in `config.yaml`, push to GitHub, and the new preset becomes selectable via the UI.

## Migration from the old `crestron_bridge` v1 add-on (Stage 7)

The old `crestron_bridge` add-on works fine for Stage 7. To migrate to this universal version:
1. Install this `crestron_bridge_universal` add-on alongside the old one (don't uninstall yet)
2. Configure with `preset: stage7`, same IP/IPID/port (8765)
3. Stop the old add-on
4. Start the new add-on
5. Verify Stage 7 functionality
6. Uninstall the old add-on once stable
