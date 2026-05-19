#!/usr/bin/with-contenv bashio
set -e

# ---- Read add-on options from HA UI configuration ----
PRO2_IP=$(bashio::config 'pro2_ip')
IPID=$(bashio::config 'ipid')
STAGE_ID=$(bashio::config 'stage_id')
HTTP_PORT=$(bashio::config 'http_port')
LOG_LEVEL=$(bashio::config 'log_level')

# ---- Export as env vars for the Python bridge ----
export PRO2_IP
export IPID
export STAGE_ID
export HTTP_PORT
export LOG_LEVEL

# ---- HA communication via supervisor proxy ----
# The supervisor automatically sets SUPERVISOR_TOKEN. Combined with the
# 'homeassistant_api: true' option in config.yaml, this lets us talk back
# to HA core at http://supervisor/core/api/* using the supervisor token.
# No need for a long-lived access token anymore.
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

bashio::log.info "Starting Crestron Bridge"
bashio::log.info "  Stage:     ${STAGE_ID}"
bashio::log.info "  Pro 2 IP:  ${PRO2_IP}"
bashio::log.info "  IPID:      0x$(printf '%02X' ${IPID})"
bashio::log.info "  HTTP port: ${HTTP_PORT}"
bashio::log.info "  Log level: ${LOG_LEVEL}"

cd /app
exec python3 crestron_bridge.py
