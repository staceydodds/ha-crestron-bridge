#!/usr/bin/with-contenv bashio
set -e

# ---- Read add-on options from HA UI configuration ----
PRESET=$(bashio::config 'preset')
PRO2_IP=$(bashio::config 'pro2_ip')
IPID=$(bashio::config 'ipid')
STAGE_ID=$(bashio::config 'stage_id')
HTTP_PORT=$(bashio::config 'http_port')
LOG_LEVEL=$(bashio::config 'log_level')
ENABLE_PROJECTOR_SERIAL=$(bashio::config 'enable_projector_serial')
ENABLE_MASKING=$(bashio::config 'enable_masking')

# ---- Export as env vars for the Python bridge ----
export PRESET
export PRO2_IP
export IPID
export STAGE_ID
export HTTP_PORT
export LOG_LEVEL
export ENABLE_PROJECTOR_SERIAL
export ENABLE_MASKING

# ---- HA communication via supervisor proxy ----
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

bashio::log.info "Starting Crestron Bridge (Universal)"
bashio::log.info "  Preset:                   ${PRESET}"
bashio::log.info "  Stage ID:                 ${STAGE_ID}"
bashio::log.info "  Pro 2 IP:                 ${PRO2_IP}"
bashio::log.info "  IPID:                     0x$(printf '%02X' ${IPID})"
bashio::log.info "  HTTP port:                ${HTTP_PORT}"
bashio::log.info "  Projector (via Crestron): ${ENABLE_PROJECTOR_SERIAL}"
bashio::log.info "  Masking module:           ${ENABLE_MASKING}"
bashio::log.info "  Log level:                ${LOG_LEVEL}"

cd /app
exec python3 crestron_bridge.py
