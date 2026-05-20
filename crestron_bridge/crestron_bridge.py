#!/usr/bin/env python3
"""
Crestron CIP Bridge — long-running HTTP service that maintains a persistent
CIP connection to a Crestron Pro 2 and exposes its functions as a REST API.

This is the add-on version: all configuration comes from environment variables
(set by run.sh from the add-on's options). One add-on instance per stage —
the STAGE_ID env var controls which HA input_number entity names this bridge
maps fader joins to.

Endpoints (all unchanged from standalone version):
  GET  /                              — health check / status
  GET  /state                         — current fader state + projector telemetry as JSON
  GET  /projector/state               — projector telemetry only
  POST /fader/<join>/<value>          — set analog fader (value 0-65535)
  POST /fader/<join>/percent/<pct>    — set analog fader by percent (0-100)
  POST /scene/<n>                     — recall scene N (1-8)
  POST /preset/<pct>                  — apply dimmer preset (0/5/25/50/75/100)
  POST /store/<n>                     — save current state to scene N (1-8)
  POST /all_off                       — turn all known faders to 0%
  POST /all_on                        — turn all known faders to 100%
  POST /projector/enable              — pulse projector enable toggle (join 199)
  POST /projector/on                  — pulse projector ON (join 202)
  POST /projector/off                 — pulse projector OFF (join 201)
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

# ---- MULTI-RECORD PATCH ----
# The 2010 Pro 2 firmware sends some message types differently than modern
# cipclient expects. Monkey-patch _processPayload to handle:
#   - Custom registration response format (\x00\x00\x00\xXX instead of magic byte)
#   - Multi-record analog/digital packets (both wrapped 0x20 0x03 and unwrapped)
#   - Serial datatype 0x15 (old firmware) instead of modern 0x12
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
        # Suppress the frequent 0x0e heartbeat — keep-alive ping, no useful info
        if ciptype != 0x0e:
            log.info(f"RX (passthrough): ciptype=0x{ciptype:02x} payload={binascii.hexlify(payload).decode()}")
        return _orig(self, ciptype, payload)

    pos = 3
    if payload[3] == 0x20 and len(payload) >= 6 and payload[4] == 0x03:
        log.info(f"RX (wrapped, dropping): payload={binascii.hexlify(payload).decode()}")
        return  # wrapped form — unwrapped duplicate handles it

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
        # Serial join (old firmware format)
        if pos + 3 > len(payload):
            return
        join = ((payload[pos] << 8) | payload[pos + 1]) + 1
        pos += 2
        pos += 1  # skip encoding marker (0x03 = ASCII)
        try:
            raw = bytes(payload[pos:])
            value = raw.decode("ascii", errors="replace").rstrip("\x00").strip()
            self.event_queue.put(("in", "s", join, value))
        except Exception as e:
            log.warning(f"Failed to decode serial join {join}: {e}")
        return

    log.info(f"RX (unknown datatype 0x{datatype:02x}): payload={binascii.hexlify(payload).decode()}")
    return _orig(self, ciptype, payload)


cipclient.CIPSocketClient._processPayload = _patched

# ---- CONFIG (read from environment variables set by run.sh) ----

PRO2_IP = os.environ.get("PRO2_IP", "192.168.1.70")
IPID = int(os.environ.get("IPID", "4"))
HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8765"))
STAGE_ID = os.environ.get("STAGE_ID", "stage7")

# HA REST API config — for forwarding state changes back to HA's input_numbers.
# In add-on mode, HA_URL is the supervisor proxy and HA_TOKEN is auto-provided
# by the supervisor (no long-lived token needed).
HA_URL = os.environ.get("HA_URL", "http://supervisor/core")
HA_TOKEN = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")

if not HA_TOKEN:
    log.warning("No HA_TOKEN / SUPERVISOR_TOKEN set — fader state forwarding to HA disabled")

# Stage fader mapping (analog joins → physical labels) — same for all broadcast stages
FADERS = {
    11: "work_lights_rear",
    12: "client_track_lights_rear",
    13: "patchbay_lights",
    14: "work_lights_middle",
    15: "credenza_lights",
    17: "work_lights_front",
    18: "console_lights",
}

# Map Pro 2 analog joins → HA input_number entities (templated by STAGE_ID).
# Stage 7 → input_number.stage7_work_rear, etc.
# Stage 5 (when deployed) → input_number.stage5_work_rear, etc.
JOIN_TO_HA_ENTITY = {
    11: f"input_number.{STAGE_ID}_work_rear",
    12: f"input_number.{STAGE_ID}_client_track",
    13: f"input_number.{STAGE_ID}_patchbay",
    14: f"input_number.{STAGE_ID}_work_mid",
    15: f"input_number.{STAGE_ID}_credenza",
    17: f"input_number.{STAGE_ID}_work_front",
    18: f"input_number.{STAGE_ID}_console",
}

# Scene digital joins (Scene N = 131 + N)
SCENE_BASE = 131

# Dimmer preset digital joins
PRESET_JOINS = {
    0: 140,
    5: 141,
    25: 142,
    50: 143,
    75: 144,
    100: 145,
}

STORE_JOIN = 131

# Projector control digital joins (validated 2026-05-15)
PROJECTOR_ENABLE_JOIN = 199
PROJECTOR_OFF_JOIN = 201
PROJECTOR_ON_JOIN = 202

# Projector telemetry analog joins
PROJECTOR_WARMUP_JOIN = 7
PROJECTOR_COOLING_JOIN = 8

# Serial join discovery range (lamp hours on join 1, input on join 2)
SERIAL_JOIN_DISCOVERY_RANGE = range(1, 51)

# Live state mirror
fader_state = {j: 0 for j in FADERS}
state_lock = threading.Lock()

# Self-echo suppression: when the bridge writes a fader value, Crestron
# echoes the new value back via the same analog join. Without this, the
# echo gets forwarded to HA's input_number, which triggers the dashboard
# automation, which POSTs to the bridge again — fighting any in-progress
# user drag and making sliders feel jerky. We track recent writes here
# and skip the forward step when an inbound value matches one we just
# sent. Tolerance is ~1% of 65535 to absorb minor Crestron rounding.
last_write = {}  # {join: (monotonic_ts, raw_value)}
last_write_lock = threading.Lock()
LAST_WRITE_SUPPRESS_SEC = 0.4
ECHO_VALUE_TOLERANCE = 656  # raw units (~1%)

# Projector telemetry state
# warming_pct / cooling_pct default to 100 (the "idle" value per Crestron
# gauge semantics — 100% = nothing happening). Defaulting these to 0
# previously caused the state machine to miss the first 100->%<100
# transition on each new bridge connect, leaving power_state desynced.
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

# Global CIP client
cip = None


def forward_state_to_ha(join, value):
    """Push the current fader value back to HA's input_number entity.
    Rounds to nearest integer percent (no 5% snap) so live drag from the
    dashboard doesn't get chunked back into 5-point increments."""
    if join not in JOIN_TO_HA_ENTITY:
        return
    if not HA_TOKEN:
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


def state_cb(sigtype, join, value):
    if join not in FADERS:
        return
    with state_lock:
        fader_state[join] = value

    # Self-echo suppression: if Crestron is echoing back a value we just
    # wrote (within the suppression window, near the value we sent), do
    # not forward to HA. This prevents the bridge from fighting an
    # in-progress user drag on the dashboard slider.
    with last_write_lock:
        entry = last_write.get(join)
    if entry is not None:
        ts, last_val = entry
        if (time.monotonic() - ts) < LAST_WRITE_SUPPRESS_SEC:
            if abs(value - last_val) <= ECHO_VALUE_TOLERANCE:
                return

    threading.Thread(
        target=forward_state_to_ha,
        args=(join, value),
        daemon=True,
    ).start()


def projector_analog_cb(sigtype, join, value):
    """Warming/cooling gauge callback with state machine.
    100% = idle, <100% = operation in progress.

    'Start' transitions (100 -> <100) gate on prior power_state to avoid
    spurious flips. 'Complete' transitions (<100 -> 100) are SELF-HEALING:
    they update power_state regardless of prior state. This means a missed
    start event (e.g., bridge restart mid-cycle, or projector toggled
    outside HA) can't permanently desync the dashboard — the next gauge
    completion will resynchronize."""
    pct = round(value / 65535 * 100) if value else 0
    pct = max(0, min(100, pct))
    with projector_state_lock:
        if join == PROJECTOR_WARMUP_JOIN:
            old_pct = projector_state["warming_pct"]
            projector_state["warming_raw"] = value
            if old_pct != pct:
                projector_state["warming_pct"] = pct
                log.info(f"Projector WARMING change: {old_pct}% -> {pct}% (raw {value})")
                if old_pct == 100 and pct < 100:
                    if projector_state["power_state"] != "warming":
                        projector_state["power_state"] = "warming"
                        log.info(f"Projector POWER STATE: -> warming (lamp ignition)")
                elif old_pct < 100 and pct == 100:
                    old_state = projector_state["power_state"]
                    if old_state != "on":
                        projector_state["power_state"] = "on"
                        log.info(f"Projector POWER STATE: {old_state} -> on (warm-up complete)")
        elif join == PROJECTOR_COOLING_JOIN:
            old_pct = projector_state["cooling_pct"]
            projector_state["cooling_raw"] = value
            if old_pct != pct:
                projector_state["cooling_pct"] = pct
                log.info(f"Projector COOLING change: {old_pct}% -> {pct}% (raw {value})")
                if old_pct == 100 and pct < 100:
                    if projector_state["power_state"] != "cooling":
                        projector_state["power_state"] = "cooling"
                        log.info(f"Projector POWER STATE: -> cooling (lamp extinguished)")
                elif old_pct < 100 and pct == 100:
                    old_state = projector_state["power_state"]
                    if old_state != "off":
                        projector_state["power_state"] = "off"
                        log.info(f"Projector POWER STATE: {old_state} -> off (cool-down complete)")


def projector_serial_cb(sigtype, join, value):
    """Parse Lamp Hours and Input serial strings. Log on change only."""
    if not isinstance(value, str):
        return
    text = value.strip()
    with projector_state_lock:
        if text.lower().startswith("lamp hours"):
            parts = text.split(":", 1) if ":" in text else text.split(None, 2)
            if len(parts) >= 2:
                new_value = parts[-1].strip()
                if projector_state["lamp_hours"] != new_value:
                    old = projector_state["lamp_hours"]
                    projector_state["lamp_hours"] = new_value
                    log.info(f"Lamp Hours change: {old!r} -> {new_value!r} (serial join {join})")
        elif text.lower().startswith("input"):
            parts = text.split(":", 1) if ":" in text else text.split(None, 1)
            if len(parts) >= 2:
                new_value = parts[-1].strip()
                if projector_state["input"] != new_value:
                    old = projector_state["input"]
                    projector_state["input"] = new_value
                    log.info(f"Input change: {old!r} -> {new_value!r} (serial join {join})")
        else:
            log.info(f"SERIAL join {join} (unrecognized): {value!r}")


def pulse(join):
    cip.set("d", join, 1)
    time.sleep(0.15)
    cip.set("d", join, 0)


def set_fader_raw(join, value):
    value = max(0, min(65535, int(value)))
    # Record this write so the inbound echo of the same value is
    # suppressed in state_cb (prevents fighting user drag).
    with last_write_lock:
        last_write[join] = (time.monotonic(), value)
    cip.set("a", join, value)


def set_fader_pct(join, pct):
    raw = int(max(0, min(100, float(pct))) / 100 * 65535)
    cip.set("a", join, raw)


def recall_scene(n):
    if 1 <= n <= 8:
        pulse(SCENE_BASE + n)
        return True
    return False


def store_to_scene(n):
    if not (1 <= n <= 8):
        return False
    pulse(STORE_JOIN)
    time.sleep(0.3)
    pulse(SCENE_BASE + n)
    return True


def apply_preset(pct):
    join = PRESET_JOINS.get(int(pct))
    if join is None:
        return False
    pulse(join)
    return True


def set_all(value):
    for j in FADERS:
        cip.set("a", j, value)


def projector_enable():
    pulse(PROJECTOR_ENABLE_JOIN)
    return True


def projector_on():
    pulse(PROJECTOR_ON_JOIN)
    return True


def projector_off():
    pulse(PROJECTOR_OFF_JOIN)
    return True


# ---- HTTP Handler ----


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, fmt, *args):
        msg = fmt % args
        if 'GET /state' in msg or 'GET /projector/state' in msg or 'GET /health' in msg:
            return
        log.info(f"HTTP {self.address_string()} - {msg}")

    def do_GET(self):
        if self.path == "/" or self.path == "/health":
            return self._send(200, {"status": "ok", "connected": cip.connected if cip else False, "stage": STAGE_ID})
        if self.path == "/state":
            with state_lock, projector_state_lock:
                return self._send(200, {
                    "stage": STAGE_ID,
                    "faders": {FADERS[j]: v for j, v in fader_state.items()},
                    "faders_by_join": dict(fader_state),
                    "projector": dict(projector_state),
                    "connected": cip.connected if cip else False,
                })
        if self.path == "/projector/state":
            with projector_state_lock:
                return self._send(200, dict(projector_state))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parts = [p for p in self.path.strip("/").split("/") if p]
        try:
            if len(parts) == 3 and parts[0] == "fader" and parts[2] == "toggle":
                join = int(parts[1])
                with state_lock:
                    current = fader_state.get(join, 0)
                new_value = 0 if current > 32767 else 65535
                set_fader_raw(join, new_value)
                return self._send(200, {"ok": True, "join": join, "new_value": new_value, "was": current})

            if len(parts) == 3 and parts[0] == "fader" and parts[2].isdigit():
                join = int(parts[1])
                value = int(parts[2])
                set_fader_raw(join, value)
                return self._send(200, {"ok": True, "join": join, "value": value})
            if len(parts) == 4 and parts[0] == "fader" and parts[2] == "percent":
                join = int(parts[1])
                pct = float(parts[3])
                set_fader_pct(join, pct)
                return self._send(200, {"ok": True, "join": join, "percent": pct})
            if len(parts) == 2 and parts[0] == "scene":
                n = int(parts[1])
                if recall_scene(n):
                    return self._send(200, {"ok": True, "scene": n})
                return self._send(400, {"error": "scene must be 1-8"})
            if len(parts) == 2 and parts[0] == "preset":
                pct = int(parts[1])
                if apply_preset(pct):
                    return self._send(200, {"ok": True, "preset": pct})
                return self._send(400, {"error": f"preset must be one of {list(PRESET_JOINS.keys())}"})
            if len(parts) == 2 and parts[0] == "store":
                n = int(parts[1])
                if store_to_scene(n):
                    return self._send(200, {"ok": True, "stored_to_scene": n})
                return self._send(400, {"error": "scene must be 1-8"})
            if self.path == "/all_off":
                set_all(0)
                return self._send(200, {"ok": True})
            if self.path == "/all_on":
                set_all(65535)
                return self._send(200, {"ok": True})
            if len(parts) == 2 and parts[0] == "projector":
                action = parts[1]
                if action == "enable":
                    projector_enable()
                    return self._send(200, {"ok": True, "projector": "enable_pulsed", "join": PROJECTOR_ENABLE_JOIN})
                if action == "on":
                    projector_on()
                    return self._send(200, {"ok": True, "projector": "on_pulsed", "join": PROJECTOR_ON_JOIN})
                if action == "off":
                    projector_off()
                    return self._send(200, {"ok": True, "projector": "off_pulsed", "join": PROJECTOR_OFF_JOIN})
                return self._send(400, {"error": "projector action must be enable, on, or off"})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            log.exception("Error handling request")
            return self._send(500, {"error": str(e)})


def main():
    global cip
    log.info(f"Starting Crestron Bridge for {STAGE_ID}")
    log.info(f"Connecting to Pro 2 at {PRO2_IP} as IPID 0x{IPID:02X}...")
    cip = cipclient.CIPSocketClient(PRO2_IP, IPID)

    # Pre-register subscriptions BEFORE cip.start() so we catch the initial state sync
    for j in FADERS:
        cip.subscribe("a", j, state_cb)

    cip.subscribe("a", PROJECTOR_WARMUP_JOIN, projector_analog_cb)
    cip.subscribe("a", PROJECTOR_COOLING_JOIN, projector_analog_cb)
    log.info(f"Pre-registered: projector gauge joins {PROJECTOR_WARMUP_JOIN} (warming) and {PROJECTOR_COOLING_JOIN} (cooling)")

    serial_registered = 0
    for j in SERIAL_JOIN_DISCOVERY_RANGE:
        try:
            cip.subscribe("s", j, projector_serial_cb)
            serial_registered += 1
        except Exception as e:
            log.debug(f"Could not subscribe to serial join {j}: {e}")
    log.info(f"Pre-registered: {serial_registered} serial joins in range {SERIAL_JOIN_DISCOVERY_RANGE.start}-{SERIAL_JOIN_DISCOVERY_RANGE.stop - 1}")

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
