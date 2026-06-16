#!/usr/bin/env bash
# setup_hotspot.sh — Set up a WiFi hotspot with captive portal for Quizzer
# Run as root: sudo bash setup_hotspot.sh [SSID] [PASSWORD] [INTERFACE] [PORT]
#
# Resolves ALL domains to this server so phones/laptops auto-detect captive portal
# and open the quiz join page in a popup browser.
# Students: NO internet access. Faculty machine: full internet (unaffected).

set -e

SSID="${1:-ClassroomQuiz}"
PASSPHRASE="${2:-quiz12345}"
WIFI_IFACE="${3:-wlan0}"
PORT="${4:-80}"
SERVER_IP="10.42.0.1"
DHCP_RANGE="10.42.0.10,10.42.0.200"

# ---- 1. Must be root ----
if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: This script must be run as root." >&2
  echo "Usage: sudo bash setup_hotspot.sh [SSID] [PASSWORD] [INTERFACE] [PORT]" >&2
  exit 1
fi

echo "============================================================"
echo "  Quizzer Hotspot Setup"
echo "  SSID       : $SSID"
echo "  Password   : $PASSPHRASE"
echo "  Interface  : $WIFI_IFACE"
echo "  Server IP  : $SERVER_IP"
echo "  App port   : $PORT"
echo "============================================================"

# ---- 2. Install dependencies if missing ----
PKGS_NEEDED=()
command -v hostapd >/dev/null 2>&1  || PKGS_NEEDED+=(hostapd)
command -v dnsmasq >/dev/null 2>&1  || PKGS_NEEDED+=(dnsmasq)

if [[ ${#PKGS_NEEDED[@]} -gt 0 ]]; then
  echo "[*] Installing: ${PKGS_NEEDED[*]}"
  apt-get update -qq
  apt-get install -y -qq "${PKGS_NEEDED[@]}"
fi

# ---- 3. Stop NetworkManager management of the interface ----
if command -v nmcli >/dev/null 2>&1; then
  echo "[*] Releasing $WIFI_IFACE from NetworkManager…"
  nmcli device set "$WIFI_IFACE" managed no 2>/dev/null || true
fi

# ---- 4. Kill existing processes ----
echo "[*] Stopping any existing hostapd / dnsmasq…"
pkill -f "hostapd /tmp/quizzer_hostapd.conf" 2>/dev/null || true
pkill -f "dnsmasq -C /tmp/quizzer_dnsmasq.conf" 2>/dev/null || true
sleep 1

# ---- 5. Configure the interface ----
echo "[*] Configuring $WIFI_IFACE with IP $SERVER_IP/24…"
ip link set "$WIFI_IFACE" up
ip addr flush dev "$WIFI_IFACE"
ip addr add "${SERVER_IP}/24" dev "$WIFI_IFACE"

# ---- 6. Write hostapd config ----
echo "[*] Writing /tmp/quizzer_hostapd.conf…"
cat > /tmp/quizzer_hostapd.conf <<HOSTAPD_EOF
interface=$WIFI_IFACE
driver=nl80211
ssid=$SSID
hw_mode=g
channel=6
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$PASSPHRASE
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
HOSTAPD_EOF

# ---- 7. Write dnsmasq config ----
echo "[*] Writing /tmp/quizzer_dnsmasq.conf…"
cat > /tmp/quizzer_dnsmasq.conf <<DNSMASQ_EOF
interface=$WIFI_IFACE
bind-interfaces
dhcp-range=$DHCP_RANGE,12h
dhcp-option=3,$SERVER_IP
dhcp-option=6,$SERVER_IP
# Resolve ALL domain names to this server — triggers captive portal detection
address=/#/$SERVER_IP
no-resolv
no-poll
DNSMASQ_EOF

# ---- 8. Start hostapd in background ----
echo "[*] Starting hostapd…"
hostapd /tmp/quizzer_hostapd.conf -B
sleep 1

# ---- 9. Start dnsmasq ----
echo "[*] Starting dnsmasq…"
dnsmasq -C /tmp/quizzer_dnsmasq.conf
sleep 1

# ---- 10. Enable IP forwarding ----
echo "[*] Enabling IP forwarding…"
sysctl -w net.ipv4.ip_forward=1

# ---- 11. iptables rules ----
# Goal: students (hotspot clients) reach ONLY this server.
#       Faculty machine (runs the server) keeps full internet via its upstream link.
#       The iptables FORWARD chain only affects traffic being routed THROUGH the machine,
#       not traffic originating FROM it — so faculty internet is unaffected.
echo "[*] Setting up iptables…"

# Flush chains we manage
iptables -F FORWARD
iptables -t nat -F PREROUTING  2>/dev/null || true
iptables -t nat -F POSTROUTING 2>/dev/null || true

# Allow already-established / related connections through
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Allow student traffic destined for this server's app port
iptables -A FORWARD -s "${SERVER_IP%.*}.0/24" -d "$SERVER_IP" -p tcp --dport "$PORT" -j ACCEPT

# DROP all other forwarding from the hotspot subnet → no internet for students
iptables -A FORWARD -s "${SERVER_IP%.*}.0/24" -j DROP

# NAT (not strictly needed since students can't reach internet, but kept for completeness)
iptables -t nat -A POSTROUTING -s "${SERVER_IP%.*}.0/24" -j MASQUERADE

# Force ALL student DNS queries to our dnsmasq (prevents hardcoded / DoH bypass)
iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p udp --dport 53 -j DNAT --to-destination "${SERVER_IP}:53"
iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p tcp --dport 53 -j DNAT --to-destination "${SERVER_IP}:53"
# Block DNS-over-TLS (port 853) so clients fall back to plain DNS through dnsmasq
iptables -A FORWARD -i "$WIFI_IFACE" -p tcp --dport 853 -j DROP
iptables -A FORWARD -i "$WIFI_IFACE" -p udp --dport 853 -j DROP

# Redirect student port-80 requests aimed at ANY IP → our server (captive portal)
iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p tcp --dport 80  -j DNAT --to-destination "${SERVER_IP}:${PORT}"
# Redirect HTTPS → our server too (shows captive portal prompt on iOS/Android)
iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p tcp --dport 443 -j DNAT --to-destination "${SERVER_IP}:${PORT}"

# Save current config for teardown reference
echo "$WIFI_IFACE" > /tmp/quizzer_iface
echo "$PORT"       > /tmp/quizzer_port

echo ""
echo "============================================================"
echo "  Hotspot '$SSID' is up!"
echo "  Server at http://$SERVER_IP:$PORT"
echo ""
echo "  Students:  connect to WiFi '$SSID' → browser opens quiz automatically"
echo "  Faculty:   internet access unchanged (upstream link unaffected)"
echo "============================================================"
