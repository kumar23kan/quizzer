#!/usr/bin/env bash
# teardown_hotspot.sh — Stop the Quizzer WiFi hotspot
# Run as root: sudo bash teardown_hotspot.sh [INTERFACE]

set -e

WIFI_IFACE="${1:-$(cat /tmp/quizzer_iface 2>/dev/null || echo wlan0)}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: This script must be run as root." >&2
  exit 1
fi

echo "[*] Stopping Quizzer hotspot on $WIFI_IFACE…"

pkill -f "hostapd /tmp/quizzer_hostapd.conf" 2>/dev/null && echo "    hostapd stopped." || echo "    hostapd was not running."
pkill -f "dnsmasq -C /tmp/quizzer_dnsmasq.conf" 2>/dev/null && echo "    dnsmasq stopped." || echo "    dnsmasq was not running."

rm -f /tmp/quizzer_hostapd.conf /tmp/quizzer_dnsmasq.conf /tmp/quizzer_iface /tmp/quizzer_port

echo "[*] Flushing iptables rules…"
iptables -F FORWARD 2>/dev/null || true
iptables -t nat -F PREROUTING  2>/dev/null || true
iptables -t nat -F POSTROUTING 2>/dev/null || true

if command -v nmcli >/dev/null 2>&1; then
  echo "[*] Returning $WIFI_IFACE to NetworkManager…"
  nmcli device set "$WIFI_IFACE" managed yes 2>/dev/null || true
  nmcli device connect "$WIFI_IFACE" 2>/dev/null || true
fi

ip addr flush dev "$WIFI_IFACE" 2>/dev/null || true
ip link set "$WIFI_IFACE" down 2>/dev/null || true

echo ""
echo "Hotspot stopped. $WIFI_IFACE returned to system control."
