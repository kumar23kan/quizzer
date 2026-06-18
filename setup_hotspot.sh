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

# ---- 3. Save current preferred connection, then release interface ----
if command -v nmcli >/dev/null 2>&1; then
  # Save whichever WiFi connection is currently active so teardown can restore it
  PREFERRED_CONN=$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null \
    | grep ":${WIFI_IFACE}$" | head -1 | cut -d: -f1 || true)
  echo "$PREFERRED_CONN" > /tmp/quizzer_preferred_conn
  echo "[*] Current connection on $WIFI_IFACE: '${PREFERRED_CONN:-none}' (saved for restore)"

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
ieee80211n=1
ht_capab=[SHORT-GI-20][DSSS_CCK-40]
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$PASSPHRASE
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
max_num_sta=200
HOSTAPD_EOF

# ---- 7. Write dnsmasq config ----
echo "[*] Writing /tmp/quizzer_dnsmasq.conf…"
cat > /tmp/quizzer_dnsmasq.conf <<DNSMASQ_EOF
interface=$WIFI_IFACE
bind-interfaces
dhcp-range=$DHCP_RANGE,1h
dhcp-lease-max=200
dhcp-option=3,$SERVER_IP
dhcp-option=6,$SERVER_IP
# Resolve ALL domain names to this server — triggers captive portal detection
address=/#/$SERVER_IP
no-resolv
no-poll
no-hosts
cache-size=0
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
# Goal: students (hotspot clients) reach ONLY this server (quiz + DNS + DHCP).
#       Faculty machine keeps full internet via its upstream link.
#       FORWARD chain only affects traffic routed THROUGH the machine.
#       QUIZZER_INPUT chain restricts what students can reach ON this machine.
echo "[*] Setting up iptables…"

# Flush / reset rules we own
iptables -F FORWARD
iptables -t nat -F PREROUTING  2>/dev/null || true
iptables -t nat -F POSTROUTING 2>/dev/null || true

# Set up named INPUT chain so we don't disturb other INPUT rules (ssh, etc.)
iptables -N QUIZZER_INPUT 2>/dev/null || iptables -F QUIZZER_INPUT
iptables -C INPUT -i "$WIFI_IFACE" -j QUIZZER_INPUT 2>/dev/null || \
  iptables -A INPUT -i "$WIFI_IFACE" -j QUIZZER_INPUT

# Allow return traffic for connections the faculty machine itself initiated
iptables -A QUIZZER_INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Allow DHCP (students need an IP address)
iptables -A QUIZZER_INPUT -p udp --dport 67 -j ACCEPT
# Allow DNS queries to dnsmasq on this machine
iptables -A QUIZZER_INPUT -p udp --dport 53 -j ACCEPT
iptables -A QUIZZER_INPUT -p tcp --dport 53 -j ACCEPT
# Allow the quiz app port
iptables -A QUIZZER_INPUT -p tcp --dport "$PORT" -j ACCEPT
# Block all other ports (SSH, etc.) from hotspot clients
iptables -A QUIZZER_INPUT -j DROP

# Allow return traffic for established connections through FORWARD
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Allow forwarding from non-hotspot interfaces (faculty machine's own internet)
iptables -A FORWARD ! -i "$WIFI_IFACE" -j ACCEPT
# DROP forwarding from hotspot subnet → no internet for students
iptables -A FORWARD -s "${SERVER_IP%.*}.0/24" -j DROP

# Force ALL student DNS queries to our dnsmasq (prevents DoH / hardcoded DNS bypass)
iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p udp --dport 53 -j DNAT --to-destination "${SERVER_IP}:53"
iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p tcp --dport 53 -j DNAT --to-destination "${SERVER_IP}:53"
# Block DNS-over-TLS (port 853)
iptables -A FORWARD -i "$WIFI_IFACE" -p tcp --dport 853 -j DROP
iptables -A FORWARD -i "$WIFI_IFACE" -p udp --dport 853 -j DROP

# Redirect student HTTP/HTTPS to quiz server → triggers captive portal detection
iptables -t nat -A PREROUTING -i "$WIFI_IFACE" -p tcp --dport 80  -j DNAT --to-destination "${SERVER_IP}:${PORT}"
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
