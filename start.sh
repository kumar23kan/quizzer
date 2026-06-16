#!/usr/bin/env bash
# Start the quiz server (run as root for port 80, or use sudo)
cd "$(dirname "$0")"
echo "============================================================"
echo "  Starting Quizzer on http://0.0.0.0:80"
echo "  Faculty dashboard : http://localhost/faculty"
echo "  Faculty password  : teacher123"
echo "  Students connect to WiFi '$SSID' and visit http://10.42.0.1/"
echo "============================================================"
python3 app.py
