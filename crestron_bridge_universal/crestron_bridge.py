#!/usr/bin/env python3
"""
Crestron CIP Bridge — Universal version.

One bridge codebase for all 8 stages. Per-stage behavior selected via:
  - PRESET env var: 'large' | 'medium' | 'small' | 'custom'
    Loads a category-specific feature set (masking mode, projector control
    path) and then a stage-specific fader map.

    Preset categories:
      large  → Stages 1, 2   (3-axis independent masking, Barco projector)
      medium → Stages 3, 4   (top+bot linked masking, Panasonic via PJLink)
      small  → Stages 5, 6, 7, 8  (no masking, NEC via Crestron serial)

    Per-stage fader maps live INSIDE each preset under "stages": each stage
    has its own subset of joins/labels populated when its on-stage walkthrough
    completes. Stages without recon get an empty fader map (bridge still
    starts; masking + scenes still work; faders show nothing in /state).

    Legacy preset names ('large_theatrical' / 'medium_theatrical' /
    'broadcast') are aliased to the new names — no add-on config changes
    required for existing production deployments.

  - STAGE_ID env var: required. Selects which entry in the preset's "stages"
    dict to load. E.g. STAGE_ID='stage4' under PRESET='medium' loads the
    Stage 4 fader map.

  - ENABLE_PROJECTOR_SERIAL / ENABLE_MASKING env vars: override feature flags.

Endpoints (registered conditionally based on enabled features):
  GET  /                              health / connected status
  GET  /state                         current fader + indicator + projector state JSON
  POST /fader/<join>/<value>          set analog fader (0-65535)
  POST /fader/<join>/percent/<pct>    set analog fader by percent (0-100)
  POST /fader/<join>/toggle           toggle fader between 0% and 100%
  POST /scene/<n>                     recall scene N (1-8)
  POST /all_off                       all faders to 0%
  POST /all_on                        all faders to 100%

  # Lighting STORE — pattern depends on preset:
  #   Stage 7 (pulse-and-recall): POST /store/<n>
  #   Stage 1 (latching toggle):  POST /store/toggle
  # Both patterns are registered; use whichever your dashboard calls.
  POST /store/<n>                     pulse store, wait, pulse scene N (Stage 7 pattern)
  POST /store/toggle                  pulse store_join — toggles SIMPL latch (Stage 1 pattern)

  # PROJECTOR (only if enable_projector_serial=true):
  POST /preset/<pct>                  dimmer preset 0/5/25/50/75/100
  POST /projector/enable              pulse projector enable
  POST /projector/on                  pulse projector ON
  POST /projector/off                 pulse projector OFF
  GET  /projector/state               projector telemetry only

  # MASKING (only if enable_masking=true):
  POST /masking/<which>/<dir>         which=top|side|bot, dir=open|close (press-and-hold)
  POST /masking/enable                pulse Masking Enable Control (latching)
  POST /masking/stop                  pulse Stop All Motion
  POST /masking/store/toggle          pulse Masking Store toggle (latching)
  POST /masking/preset/<n>            recall masking preset N (1-4)
"""

import binascii
import json
import logging
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import cipclient

# ---- LOGGING ----
_log_level_str = os.environ.get("LOG_LEVEL", "info").upper()
_log_level = getattr(logging, _log_level_str, logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
)
log = logging.getLogger("bridge")

# ---- FIRMWARE QUIRK PATCH ----
# 2010 Pro 2 firmware sends some message types differently than modern
# cipclient expects. Patch _processPayload to handle the multi-record
# and legacy serial formats. (Identical across both stage bridges.)
_orig = cipclient.CIPSocketClient._processPayload


def _patched(self, ciptype, payload):
    if ciptype == 0x02:
        length = len(payload)
        ipid = str(binascii.hexlify(self.ipid), "ascii")
        if length == 4 and payload[:3] == b"\x00\x00\x00":
            log.info(f"Registered IPID 0x{ipid}")
            self.tx_queue.put(b"\x05\x00\x05\x00\x00\x02\x03\x00")
            return

    if ciptype != 0x05 or len(payload) < 4:
        if ciptype != 0x0e:
            log.debug(f"RX (passthrough): ciptype=0x{ciptype:02x} payload={binascii.hexlify(payload).decode()}")
        return _orig(self, ciptype, payload)

    pos = 3
    if payload[3] == 0x20 and len(payload) >= 6 and payload[4] == 0x03:
        return  # wrapped form — dropped (unwrapped duplicate handles it)

    if pos >= len(payload):
        return

    datatype = payload[pos]
    pos += 1

    if datatype == 0x14:
        while pos + 4 <= len(payload):
            join = ((payload[pos] << 8) | payload[pos + 1]) + 1
            value = (payload[pos + 2] << 8) | payload[pos + 3]
            self.event_queue.put(("in", "a", join, value))
            pos += 4
        return
    elif datatype == 0x00:
        while pos + 2 <= len(payload):
            join = (((payload[pos + 1] & 0x7F) << 8) | payload[pos]) + 1
            state = ((payload[pos + 1] & 0x80) >> 7) ^ 0x01
            self.event_queue.put(("in", "d", join, state))
            pos += 2
        return
    elif datatype == 0x15:
        if pos + 3 > len(payload):
            return
        join = ((payload[pos] << 8) | payload[pos + 1]) + 1
        pos += 2
        pos += 1
        try:
            raw = bytes(payload[pos:])
            value = raw.decode("ascii", errors="replace").rstrip("\x00").strip()
            self.event_queue.put(("in", "s", join, value))
        except Exception as e:
            log.warning(f"Failed to decode serial join {join}: {e}")
        return

    log.debug(f"RX (unknown datatype 0x{datatype:02x}): payload={binascii.hexlify(payload).decode()}")
    return _orig(self, ciptype, payload)


cipclient.CIPSocketClient._processPayload = _patched

# ---- PRESETS ----
# Per-stage fader maps and default feature flags, organized by room category.
#
# CATEGORIES (each maps to a "type of room" not a specific stage):
#   large  → Stages 1, 2   (full theatrical rigs, 3-axis independent masking, Barco projector)
#   medium → Stages 3, 4   (smaller theatrical rigs, linked top+bot masking, Panasonic via PJLink)
#   small  → Stages 5-8    (broadcast/voiceover rigs, no masking, NEC via Crestron serial)
#   custom → no built-in faders, no built-in features (manual config required)
#
# PER-STAGE MAPS:
# Each preset has a "stages" dict keyed by STAGE_ID. The bridge looks up the
# fader map for THIS stage at startup: PRESETS[PRESET]["stages"][STAGE_ID].
# Adding a new stage = adding one entry under the right preset.
#
# RESERVED JOINS:
# All theatrical/medium SIMPL programs publish 24 analog joins (10-33). Some
# of those joins are wired to real fixtures; others are SIMPL ghost channels
# (legacy fixture banks that were removed but the signals never cleaned up).
# Per-stage maps include ALL joins, with real fixtures labeled by fixture
# name and unwired joins labeled "_reserved_NN". Labels starting with "_" are
# SUBSCRIBED for diagnostic visibility but NOT forwarded to HA entities —
# the bridge's forward_state_to_ha() filters them out. This preserves
# future-expansion headroom (drop a real fixture name into a _reserved slot
# when one is added) without polluting HA logs.
#
# BACKWARD COMPAT:
# The old preset names (large_theatrical / medium_theatrical / broadcast) are
# aliased to the new names below — see PRESET_ALIASES — so existing add-on
# configs on Stages 1, 7 keep working without simultaneous fleet update.
PRESETS = {
    "large": {
        "default_features": {"projector_serial": False, "masking": True},
        "masking_mode": "independent",  # 3 independent axes (top, side, bot)
        "stages": {
            "stage1": {
                "faders": {
                    10: "square_floor_lights",
                    11: "client_center",
                    12: "game_left",
                    13: "patch_bay",
                    14: "editor_right",
                    15: "editor_left",
                    16: "game_right",
                    17: "credenza",
                    18: "side_step_lights",
                    19: "work_rear",
                    20: "work_middle",
                    21: "client_wide",
                    22: "sconces_rear_upper",
                    23: "sconces_rear_lower",
                    24: "pony_front",
                    25: "work_front",
                    26: "sconces_mid_lower",
                    27: "sconces_mid_upper",
                    28: "client_spots",
                    29: "wall_wash",
                    30: "pony_rear",
                    31: "sconces_front_upper",
                    32: "sconces_front_lower",
                    33: "console_spots",
                },
            },
            "stage2": {
                # Walkthrough PENDING. Stage 2 is expected to mirror Stage 1's
                # layout (built from the same template), but verify on-stage
                # before populating. For now, faders empty = bridge runs with
                # masking + scenes only until Stage 2 recon completes.
                "faders": {},
            },
        },
    },
    "medium": {
        "default_features": {"projector_serial": False, "masking": True},
        "masking_mode": "linked_top_bottom",  # top + bot axes linked at SIMPL; side separate
        "stages": {
            "stage3": {
                # Walkthrough PENDING. Operator noted Stage 3 has MORE than 13
                # working fixtures — closer to Stage 1's count, possibly 16-20.
                # Don't presume the layout from Stage 4's. Run the walkthrough
                # script (cip-scripts/Stage4-Fader-Walkthrough-V2.sh adapted)
                # before populating.
                "faders": {},
            },
            "stage4": {
                # Discovered via on-stage recon 2026-06-16.
                # 13 working fixtures + 11 _reserved (SIMPL ghost channels
                # from pre-2025 sconces upper/lower banks).
                "faders": {
                    10: "client_center",
                    11: "client_spots",
                    12: "client_wide",
                    13: "door_spots",
                    14: "patch_bay",
                    15: "credenza",
                    16: "console_spots",
                    17: "_reserved_17",
                    18: "work_middle",
                    19: "work_front",
                    20: "square_floor_lights",
                    21: "step_lights",
                    22: "_reserved_22",
                    23: "sconces",
                    24: "pony_wall",
                    25: "_reserved_25",
                    26: "_reserved_26",
                    27: "_reserved_27",
                    28: "_reserved_28",
                    29: "_reserved_29",
                    30: "_reserved_30",
                    31: "_reserved_31",
                    32: "_reserved_32",
                    33: "_reserved_33",
                },
            },
        },
    },
    "small": {
        "default_features": {"projector_serial": True, "masking": False},
        "masking_mode": "none",
        "stages": {
            "stage5": {
                # Walkthrough PENDING. Expected to mirror Stage 7's broadcast
                # layout but verify on-stage. Operator chose "Option A":
                # subscribe to all 24 joins for future expansion.
                "faders": {},
            },
            "stage6": {
                "faders": {},  # walkthrough pending
            },
            "stage7": {
                # Currently 7 known faders from initial broadcast recon.
                # Operator chose Option A for small/broadcast: subscribe to
                # all 24 joins. Stage 7's existing production keeps working
                # with these 7; remaining 17 joins become _reserved when a
                # re-walkthrough confirms what's actually wired/unwired in
                # Stage 7's SIMPL. Until then, leaving at 7 is correct —
                # the bridge subscribes only to declared joins.
                "faders": {
                    11: "work_rear",
                    12: "client_track",
                    13: "patchbay",
                    14: "work_mid",
                    15: "credenza",
                    17: "work_front",
                    18: "console",
                },
            },
            "stage8": {
                "faders": {},  # walkthrough pending
            },
        },
    },
    "custom": {
        "default_features": {"projector_serial": False, "masking": False},
        "masking_mode": "none",
        "stages": {},  # custom users define their own faders via env or override
    },
}

# Backward-compat aliases for stages that haven't yet migrated to the new
# preset names. These resolve to the new names at startup; functionally
# identical to setting the new name in add-on config.
PRESET_ALIASES = {
    "large_theatrical": "large",
    "medium_theatrical": "medium",
    "broadcast": "small",
}

# ---- CONFIG (read from environment variables set by run.sh) ----

PRESET = os.environ.get("PRESET", "large_theatrical").lower()
PRO2_IP = os.environ.get("PRO2_IP", "10.12.7.15")
IPID = int(os.environ.get("IPID", "5"))
HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8766"))
STAGE_ID = os.environ.get("STAGE_ID", "stage1")

ENABLE_PROJECTOR_SERIAL = os.environ.get("ENABLE_PROJECTOR_SERIAL", "false").lower() == "true"
ENABLE_MASKING = os.environ.get("ENABLE_MASKING", "false").lower() == "true"

# HA REST API config (supervisor proxy in add-on mode)
HA_URL = os.environ.get("HA_URL", "http://supervisor/core")
HA_TOKEN = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")

if not HA_TOKEN:
    log.warning("No HA_TOKEN / SUPERVISOR_TOKEN set — fader state forwarding to HA disabled")

# Apply backward-compat alias if the configured preset name is an old one
if PRESET in PRESET_ALIASES:
    new_preset = PRESET_ALIASES[PRESET]
    log.info(f"Preset name '{PRESET}' is aliased to '{new_preset}' — using new name")
    PRESET = new_preset

if PRESET not in PRESETS:
    log.error(
        f"Unknown preset '{PRESET}'. "
        f"Valid: {list(PRESETS.keys())} "
        f"(or legacy aliases: {list(PRESET_ALIASES.keys())})"
    )
    sys.exit(1)

preset_data = PRESETS[PRESET]

# Look up THIS stage's fader map from the preset's stages dict
stage_data = preset_data.get("stages", {}).get(STAGE_ID, {})
if not stage_data:
    log.warning(
        f"No stage-specific data for STAGE_ID='{STAGE_ID}' under preset '{PRESET}'. "
        f"Bridge will start with 0 faders (masking/scenes still functional). "
        f"Add an entry under PRESETS['{PRESET}']['stages']['{STAGE_ID}'] when "
        f"the walkthrough for this stage completes."
    )

FADERS = dict(stage_data.get("faders", {}))

# Expose masking_mode for any downstream code that wants to act on it
# (today: informational only; future: could gate /masking/bot/* on
# linked_top_bottom and return a clearer 4xx instead of pulsing dead joins).
MASKING_MODE = preset_data.get("masking_mode", "independent")

# If env explicitly says enable/disable a feature, that wins. Otherwise use preset default.
if "ENABLE_PROJECTOR_SERIAL" not in os.environ:
    ENABLE_PROJECTOR_SERIAL = preset_data["default_features"]["projector_serial"]
if "ENABLE_MASKING" not in os.environ:
    ENABLE_MASKING = preset_data["default_features"]["masking"]

JOIN_TO_HA_ENTITY = {
    j: f"input_number.{STAGE_ID}_{name}"
    for j, name in FADERS.items()
}

log.info(f"Universal bridge starting")
log.info(f"  Preset:    {PRESET} ({len(FADERS)} faders)")
log.info(f"  Stage ID:  {STAGE_ID}")
log.info(f"  Projector: {'enabled' if ENABLE_PROJECTOR_SERIAL else 'disabled'}")
log.info(f"  Masking:   {'enabled' if ENABLE_MASKING else 'disabled'}")

# ---- LIGHTING ----
# Scene digital joins (Scene N pulses 131+N — common to both stage layouts)
SCENE_BASE = 131
STORE_JOIN = 131  # Lighting store (latching for Stage 1, used for store-then-scene on Stage 7)
STORE_ARMED_INDICATOR_JOIN = 9  # analog, value>0 = lighting store latch armed (Stage 1)

# Dimmer presets (Stage 7 only — but harmless to define)
PRESET_JOINS = {0: 140, 5: 141, 25: 142, 50: 143, 75: 144, 100: 145}

# ---- PROJECTOR (Stage 7) ----
PROJECTOR_ENABLE_JOIN = 199
PROJECTOR_OFF_JOIN = 201
PROJECTOR_ON_JOIN = 202
PROJECTOR_WARMUP_JOIN = 7
PROJECTOR_COOLING_JOIN = 8
SERIAL_JOIN_DISCOVERY_RANGE = range(1, 51)

# ---- MASKING (Stage 1) ----
MASKING_DIGITALS = {
    ("top",    "open"):  160,
    ("top",    "close"): 161,
    ("side",   "close"): 162,
    ("side",   "open"):  163,
    ("bot",    "close"): 164,
    ("bot",    "open"):  165,
    ("bottom", "close"): 164,
    ("bottom", "open"):  165,
}
MASKING_ENABLE_JOIN = 168
MASKING_STOP_JOIN = 170
MASKING_STORE_JOIN = 171
MASKING_STORE_ARMED_INDICATOR_JOIN = 8
MASKING_PRESET_JOINS = {1: 172, 2: 173, 3: 174, 4: 175}

# ---- STATE MIRROR ----
fader_state = {j: 0 for j in FADERS}
state_lock = threading.Lock()

indicator_state = {
    "lighting_store_armed": False,
    "masking_store_armed": False,
    "masking_enable_armed": False,
    "masking_active_preset": None,
}
indicator_lock = threading.Lock()

projector_state = {
    "lamp_hours": None,
    "input": None,
    "warming_pct": 100,
    "cooling_pct": 100,
    "warming_raw": 0,
    "cooling_raw": 0,
    "power_state": "off",
}
projector_state_lock = threading.Lock()

# Self-echo suppression
last_write = {}
last_write_lock = threading.Lock()
LAST_WRITE_SUPPRESS_SEC = 0.4
ECHO_VALUE_TOLERANCE = 656

# Masking press-and-hold
masking_press_deadlines = {}
masking_press_lock = threading.Lock()
MASKING_PRESS_TIMEOUT_SEC = 0.3

cip = None


# ---- HA FORWARDING ----

def forward_state_to_ha(join, value):
    """Push fader value back to HA input_number for slider sync.

    Skips joins whose fader label starts with "_" — those are RESERVED slots
    (SIMPL channels with no physical fixture). They're subscribed for
    diagnostic visibility in /state but should never be forwarded to HA
    because no input_number entity exists for them and we'd just log
    warnings on every value change.
    """
    if join not in JOIN_TO_HA_ENTITY:
        return
    if not HA_TOKEN:
        return
    if FADERS.get(join, "").startswith("_"):
        return
    entity_id = JOIN_TO_HA_ENTITY[join]
    pct = round(value / 65535 * 100)
    pct = max(0, min(100, pct))
    try:
        url = f"{HA_URL}/api/services/input_number/set_value"
        payload = json.dumps({"entity_id": entity_id, "value": pct}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {HA_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception as e:
        log.warning(f"Failed to forward state to HA for join {join}: {e}")


# ---- CALLBACKS ----

def fader_cb(sigtype, join, value):
    if join not in FADERS:
        return
    with state_lock:
        fader_state[join] = value

    with last_write_lock:
        entry = last_write.get(join)
    if entry is not None:
        ts, last_val = entry
        if (time.monotonic() - ts) < LAST_WRITE_SUPPRESS_SEC:
            if abs(value - last_val) <= ECHO_VALUE_TOLERANCE:
                return
    threading.Thread(target=forward_state_to_ha, args=(join, value), daemon=True).start()


def indicator_analog_cb(sigtype, join, value):
    """Tracks store-armed indicator joins (analog 8 and 9)."""
    with indicator_lock:
        if join == STORE_ARMED_INDICATOR_JOIN:
            old = indicator_state["lighting_store_armed"]
            new = value > 0
            if old != new:
                indicator_state["lighting_store_armed"] = new
                log.info(f"Lighting STORE armed: {old} -> {new}")
        elif join == MASKING_STORE_ARMED_INDICATOR_JOIN:
            old = indicator_state["masking_store_armed"]
            new = value > 0
            if old != new:
                indicator_state["masking_store_armed"] = new
                log.info(f"Masking STORE armed: {old} -> {new}")


def masking_preset_cb(sigtype, join, value):
    """Tracks which masking preset is currently active via SIMPL feedback."""
    if value != 1:
        return
    with indicator_lock:
        for n, j in MASKING_PRESET_JOINS.items():
            if join == j:
                old = indicator_state["masking_active_preset"]
                if old != n:
                    indicator_state["masking_active_preset"] = n
                    log.info(f"Masking active preset: {old} -> {n}")
                return


def masking_enable_cb(sigtype, join, value):
    """Tracks Masking Enable Control latch state (digital 168)."""
    if join != MASKING_ENABLE_JOIN:
        return
    new_state = bool(value)
    with indicator_lock:
        old = indicator_state["masking_enable_armed"]
        if old != new_state:
            indicator_state["masking_enable_armed"] = new_state
            log.info(f"Masking Enable Control: {old} -> {new_state}")


def projector_analog_cb(sigtype, join, value):
    """Warming/cooling gauge state machine. 100% = idle, <100% = in progress."""
    pct = round(value / 65535 * 100) if value else 0
    pct = max(0, min(100, pct))
    with projector_state_lock:
        if join == PROJECTOR_WARMUP_JOIN:
            old_pct = projector_state["warming_pct"]
            projector_state["warming_raw"] = value
            if old_pct != pct:
                projector_state["warming_pct"] = pct
                if old_pct == 100 and pct < 100:
                    if projector_state["power_state"] != "warming":
                        projector_state["power_state"] = "warming"
                        log.info(f"Projector POWER STATE: -> warming")
                elif old_pct < 100 and pct == 100:
                    if projector_state["power_state"] != "on":
                        projector_state["power_state"] = "on"
                        log.info(f"Projector POWER STATE: -> on")
        elif join == PROJECTOR_COOLING_JOIN:
            old_pct = projector_state["cooling_pct"]
            projector_state["cooling_raw"] = value
            if old_pct != pct:
                projector_state["cooling_pct"] = pct
                if old_pct == 100 and pct < 100:
                    if projector_state["power_state"] != "cooling":
                        projector_state["power_state"] = "cooling"
                        log.info(f"Projector POWER STATE: -> cooling")
                elif old_pct < 100 and pct == 100:
                    if projector_state["power_state"] != "off":
                        projector_state["power_state"] = "off"
                        log.info(f"Projector POWER STATE: -> off")


def projector_serial_cb(sigtype, join, value):
    """Parse Lamp Hours and Input serial strings."""
    if not isinstance(value, str):
        return
    text = value.strip()
    with projector_state_lock:
        if text.lower().startswith("lamp hours"):
            parts = text.split(":", 1) if ":" in text else text.split(None, 2)
            if len(parts) >= 2:
                new_value = parts[-1].strip()
                if projector_state["lamp_hours"] != new_value:
                    projector_state["lamp_hours"] = new_value
                    log.info(f"Lamp Hours: {new_value!r}")
        elif text.lower().startswith("input"):
            parts = text.split(":", 1) if ":" in text else text.split(None, 1)
            if len(parts) >= 2:
                new_value = parts[-1].strip()
                if projector_state["input"] != new_value:
                    projector_state["input"] = new_value
                    log.info(f"Input: {new_value!r}")


# ---- CIP HELPERS ----

def pulse(join):
    cip.set("d", join, 1)
    time.sleep(0.15)
    cip.set("d", join, 0)


def set_fader_raw(join, value):
    value = max(0, min(65535, int(value)))
    with last_write_lock:
        last_write[join] = (time.monotonic(), value)
    cip.set("a", join, value)


def set_fader_pct(join, pct):
    raw = int(max(0, min(100, float(pct))) / 100 * 65535)
    set_fader_raw(join, raw)


def recall_scene(n):
    if 1 <= n <= 8:
        pulse(SCENE_BASE + n)
        return True
    return False


def store_to_scene(n):
    """Stage 7 pattern: pulse store, wait, pulse scene."""
    if not (1 <= n <= 8):
        return False
    pulse(STORE_JOIN)
    time.sleep(0.3)
    pulse(SCENE_BASE + n)
    return True


def store_toggle():
    """Stage 1 pattern: pulse store_join — toggles SIMPL latch."""
    pulse(STORE_JOIN)


def apply_preset(pct):
    join = PRESET_JOINS.get(int(pct))
    if join is None:
        return False
    pulse(join)
    return True


def set_all(value):
    for j in FADERS:
        set_fader_raw(j, value)


def projector_enable():
    pulse(PROJECTOR_ENABLE_JOIN)
    return True


def projector_on():
    pulse(PROJECTOR_ON_JOIN)
    return True


def projector_off():
    pulse(PROJECTOR_OFF_JOIN)
    return True


def masking_press_extend(join):
    """Set digital HIGH (if not already) and extend the auto-release deadline.
    Watchdog drops the digital when deadline elapses without renewal."""
    now = time.monotonic()
    with masking_press_lock:
        already_active = (join in masking_press_deadlines
                          and now < masking_press_deadlines[join])
        masking_press_deadlines[join] = now + MASKING_PRESS_TIMEOUT_SEC
    if not already_active:
        cip.set("d", join, 1)
        log.info(f"Masking press: digital {join} = HIGH (auto-releases in {MASKING_PRESS_TIMEOUT_SEC}s)")
        threading.Thread(target=_masking_watchdog, args=(join,), daemon=True).start()


def _masking_watchdog(join):
    while True:
        time.sleep(0.05)
        with masking_press_lock:
            deadline = masking_press_deadlines.get(join)
            if deadline is None:
                return
            if time.monotonic() >= deadline:
                del masking_press_deadlines[join]
                break
    cip.set("d", join, 0)
    log.info(f"Masking release: digital {join} = LOW")


def masking_direct(which, direction):
    join = MASKING_DIGITALS.get((which.lower(), direction.lower()))
    if join is None:
        return False
    masking_press_extend(join)
    return True


def masking_enable():
    pulse(MASKING_ENABLE_JOIN)
    return True


def masking_stop():
    pulse(MASKING_STOP_JOIN)
    return True


def masking_store_toggle():
    pulse(MASKING_STORE_JOIN)
    return True


def masking_preset(n):
    join = MASKING_PRESET_JOINS.get(int(n))
    if join is None:
        return False
    pulse(join)
    return True


# ---- HTTP HANDLER ----

class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "GET /state" in msg or "GET /health" in msg or "GET /projector/state" in msg:
            return
        log.info(f"HTTP {self.address_string()} - {msg}")

    def do_GET(self):
        if self.path in ("/", "/health"):
            return self._send(200, {
                "status": "ok",
                "connected": cip.connected if cip else False,
                "stage": STAGE_ID,
                "preset": PRESET,
                "features": {"projector": ENABLE_PROJECTOR_SERIAL, "masking": ENABLE_MASKING},
            })
        if self.path == "/state":
            with state_lock, indicator_lock, projector_state_lock:
                body = {
                    "stage": STAGE_ID,
                    "preset": PRESET,
                    "faders": {FADERS[j]: v for j, v in fader_state.items()},
                    "faders_by_join": dict(fader_state),
                    "connected": cip.connected if cip else False,
                }
                if ENABLE_MASKING:
                    body["indicators"] = dict(indicator_state)
                if ENABLE_PROJECTOR_SERIAL:
                    body["projector"] = dict(projector_state)
                return self._send(200, body)
        if self.path == "/projector/state" and ENABLE_PROJECTOR_SERIAL:
            with projector_state_lock:
                return self._send(200, dict(projector_state))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parts = [p for p in self.path.strip("/").split("/") if p]
        try:
            # ---- FADER ----
            if len(parts) == 3 and parts[0] == "fader" and parts[2] == "toggle":
                join = int(parts[1])
                with state_lock:
                    current = fader_state.get(join, 0)
                new_value = 0 if current > 32767 else 65535
                set_fader_raw(join, new_value)
                return self._send(200, {"ok": True, "join": join, "new_value": new_value, "was": current})

            if len(parts) == 3 and parts[0] == "fader" and parts[2].isdigit():
                set_fader_raw(int(parts[1]), int(parts[2]))
                return self._send(200, {"ok": True, "join": int(parts[1]), "value": int(parts[2])})

            if len(parts) == 4 and parts[0] == "fader" and parts[2] == "percent":
                set_fader_pct(int(parts[1]), float(parts[3]))
                return self._send(200, {"ok": True, "join": int(parts[1]), "percent": float(parts[3])})

            # ---- SCENE ----
            if len(parts) == 2 and parts[0] == "scene":
                n = int(parts[1])
                if recall_scene(n):
                    return self._send(200, {"ok": True, "scene": n})
                return self._send(400, {"error": "scene must be 1-8"})

            # ---- STORE (both patterns supported) ----
            if len(parts) == 2 and parts[0] == "store" and parts[1] == "toggle":
                store_toggle()
                return self._send(200, {"ok": True, "store": "toggled"})
            if len(parts) == 2 and parts[0] == "store" and parts[1].isdigit():
                n = int(parts[1])
                if store_to_scene(n):
                    return self._send(200, {"ok": True, "stored_to_scene": n})
                return self._send(400, {"error": "scene must be 1-8"})

            # ---- ALL ON / OFF ----
            if self.path == "/all_off":
                set_all(0)
                return self._send(200, {"ok": True})
            if self.path == "/all_on":
                set_all(65535)
                return self._send(200, {"ok": True})

            # ---- PROJECTOR (only if enabled) ----
            if ENABLE_PROJECTOR_SERIAL:
                if len(parts) == 2 and parts[0] == "preset":
                    pct = int(parts[1])
                    if apply_preset(pct):
                        return self._send(200, {"ok": True, "preset": pct})
                    return self._send(400, {"error": f"preset must be one of {list(PRESET_JOINS.keys())}"})
                if len(parts) == 2 and parts[0] == "projector":
                    action = parts[1]
                    if action == "enable":
                        projector_enable()
                        return self._send(200, {"ok": True, "projector": "enable_pulsed"})
                    if action == "on":
                        projector_on()
                        return self._send(200, {"ok": True, "projector": "on_pulsed"})
                    if action == "off":
                        projector_off()
                        return self._send(200, {"ok": True, "projector": "off_pulsed"})
                    return self._send(400, {"error": "projector action must be enable/on/off"})

            # ---- MASKING (only if enabled) ----
            if ENABLE_MASKING:
                if len(parts) >= 2 and parts[0] == "masking":
                    sub = parts[1]
                    if sub == "enable":
                        masking_enable()
                        return self._send(200, {"ok": True, "masking": "enable_pulsed"})
                    if sub == "stop":
                        masking_stop()
                        return self._send(200, {"ok": True, "masking": "stop_pulsed"})
                    if sub == "store" and len(parts) == 3 and parts[2] == "toggle":
                        masking_store_toggle()
                        return self._send(200, {"ok": True, "masking_store": "toggled"})
                    if sub == "preset" and len(parts) == 3:
                        n = int(parts[2])
                        if masking_preset(n):
                            return self._send(200, {"ok": True, "masking_preset": n})
                        return self._send(400, {"error": "preset must be 1-4"})
                    if sub in ("top", "side", "bot", "bottom") and len(parts) == 3:
                        direction = parts[2]
                        if masking_direct(sub, direction):
                            return self._send(200, {"ok": True, "masking": f"{sub}_{direction}"})
                        return self._send(400, {"error": "direction must be open or close"})

            return self._send(404, {"error": "not found"})
        except Exception as e:
            log.exception("Error handling request")
            return self._send(500, {"error": str(e)})


def main():
    global cip
    log.info(f"Connecting to Pro 2 at {PRO2_IP} as IPID 0x{IPID:02X}...")
    cip = cipclient.CIPSocketClient(PRO2_IP, IPID)

    # ---- SUBSCRIBE: faders (always) ----
    for j in FADERS:
        cip.subscribe("a", j, fader_cb)
    log.info(f"Subscribed: {len(FADERS)} fader joins ({min(FADERS) if FADERS else '-'}-{max(FADERS) if FADERS else '-'})")

    # ---- SUBSCRIBE: lighting STORE indicator (always — Stage 1 needs it for armed sync) ----
    cip.subscribe("a", STORE_ARMED_INDICATOR_JOIN, indicator_analog_cb)

    # ---- SUBSCRIBE: projector telemetry (conditional) ----
    if ENABLE_PROJECTOR_SERIAL:
        cip.subscribe("a", PROJECTOR_WARMUP_JOIN, projector_analog_cb)
        cip.subscribe("a", PROJECTOR_COOLING_JOIN, projector_analog_cb)
        log.info(f"Subscribed: projector gauges (a{PROJECTOR_WARMUP_JOIN} warming, a{PROJECTOR_COOLING_JOIN} cooling)")
        serial_registered = 0
        for j in SERIAL_JOIN_DISCOVERY_RANGE:
            try:
                cip.subscribe("s", j, projector_serial_cb)
                serial_registered += 1
            except Exception:
                pass
        log.info(f"Subscribed: {serial_registered} serial joins for projector telemetry")

    # ---- SUBSCRIBE: masking indicators (conditional) ----
    if ENABLE_MASKING:
        cip.subscribe("a", MASKING_STORE_ARMED_INDICATOR_JOIN, indicator_analog_cb)
        for n, j in MASKING_PRESET_JOINS.items():
            cip.subscribe("d", j, masking_preset_cb)
        cip.subscribe("d", MASKING_ENABLE_JOIN, masking_enable_cb)
        log.info(f"Subscribed: masking indicators "
                 f"(a{MASKING_STORE_ARMED_INDICATOR_JOIN} store, d{MASKING_ENABLE_JOIN} enable, "
                 f"d{list(MASKING_PRESET_JOINS.values())} presets)")

    cip.start()
    time.sleep(5)

    if not cip.connected:
        log.error("Failed to connect to Pro 2. Exiting.")
        cip.stop()
        return 1

    log.info(f"Connected. HTTP server listening on http://{HTTP_HOST}:{HTTP_PORT}")
    server = HTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        cip.stop()


if __name__ == "__main__":
    sys.exit(main() or 0)
