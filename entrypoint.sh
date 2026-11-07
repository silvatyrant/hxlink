#!/bin/sh
set -e

echo ""
echo "💡💡  HXLINK – MQTT → BLE bridge for Hxlight bulbs  💡💡"
echo ""
echo "🐍  Python     : $(python --version 2>&1)"
echo "🔌  MQTT       : ${MQTT_HOST:-?}:${MQTT_PORT:-1883}"
echo "💡  Bulbs      : ${BULBS:-${BULB_NAME:-1 bulb}}"
echo "📡  Adapter    : hci${HCI:-0}, hold ${HOLD_MS:-350}ms"
echo "💾  State      : ${HXLINK_STATE:-/data/state.json}"
echo ""

exec python -m hxlink.bridge
