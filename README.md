# Quizzer

A self-hosted classroom quiz system that runs entirely offline over a local WiFi hotspot. The teacher's laptop becomes the quiz server; students connect via phone or tablet — no internet required.

## Features

- **AI question generation** via [Ollama](https://ollama.com) (local LLM, no cloud)
- **AI chat assistant** — ask in plain English to add or refine questions
- **Manual question entry** with optional image upload (MCQ or True/False)
- **Delete individual questions** from the bank at any time
- **Per-question time limits** (AI-suggested, faculty-editable)
- **Anti-copy randomisation** — every student gets the same questions in a different order
- **WiFi hotspot with captive portal** — students' browsers open the quiz automatically on connect
- **Student internet blocked** via iptables; teacher's machine keeps its own connection
- **Live progress dashboard** — see each student's answered/correct count in real time
- **Timer extension** mid-quiz (+5/+10/+15 min or custom)
- **Results leaderboard** with CSV export

## Requirements

| Dependency | Version |
|---|---|
| Python | 3.10+ |
| Flask | ≥ 2.3 |
| Flask-SocketIO | ≥ 5.3 |
| gevent / gevent-websocket | ≥ 23.9 / 0.10 |
| requests | ≥ 2.31 |
| Ollama | any recent build |
| hostapd + dnsmasq | (hotspot only, Linux) |

Install Python dependencies:

```bash
pip install -r requirements.txt
```

## Quick Start (local testing)

```bash
# 1. Start Ollama and pull a model
ollama pull llama3

# 2. Run the server on port 5000
PORT=5000 python3 app.py

# 3. Open the faculty dashboard
#    http://localhost:5000/faculty   (password: teacher123)
```

Students open `http://localhost:5000/` in the same machine for local testing.

## Classroom Deployment (WiFi hotspot)

### 1. Install sudoers rule (one-time)

```bash
sudo cp quizzer-sudoers /etc/sudoers.d/quizzer
sudo chmod 440 /etc/sudoers.d/quizzer
```

### 2. Start the server on port 80

```bash
sudo python3 app.py        # runs on port 80 by default
# or
sudo bash start.sh
```

### 3. Deploy the hotspot from the faculty UI

Log in at `http://localhost/faculty`, then use the **Configure Hotspot** button in the status bar to set the network name and password and click **Deploy Hotspot**.

Students connect to the WiFi network → their browser opens the quiz join page automatically (captive portal). If the auto-open doesn't appear, they can open any browser and go to `10.42.0.1`.

> **Note:** The hotspot uses the machine's WiFi card in access-point mode. If your internet connection is also via WiFi, plug in an ethernet cable first — the hotspot will disconnect WiFi clients.

### 4. Manual hotspot control (optional)

```bash
# start
sudo bash setup_hotspot.sh "MyQuiz" "password123" wlan0 80

# stop
sudo bash teardown_hotspot.sh
```

## Faculty Workflow

| Step | Action |
|---|---|
| 1 | Enter topic → **Generate Questions** (AI fills the bank) |
| 2 | Edit questions, adjust time limits, attach images, or remove unwanted ones (✕ button) |
| 3 | Use the **AI Assistant** to add more questions in plain English |
| 4 | Click **Proceed to Lobby** → students can now join |
| 5 | Click **Start Quiz** when everyone is in |
| 6 | Monitor live progress; use **Extend Timer** if needed |
| 7 | View results → **Download CSV** |

## Security Notes

- Change `FACULTY_PASSWORD` in `app.py` before deploying.
- The server is intended for isolated LAN use only — do not expose it to the public internet.
- `quiz.db` and `static/uploads/` are gitignored and never committed.

## Project Structure

```
quizzer/
├── app.py                  # Flask backend, Socket.IO events, REST API
├── ai_generator.py         # Ollama question generation and AI chat
├── requirements.txt
├── setup_hotspot.sh        # hostapd + dnsmasq + iptables hotspot setup
├── teardown_hotspot.sh     # hotspot teardown
├── start.sh                # convenience launcher
├── quizzer-sudoers         # sudoers rule for passwordless hotspot scripts
├── static/
│   ├── socket.io.js        # bundled Socket.IO v4 client (works fully offline)
│   └── uploads/            # question images (gitignored)
└── templates/
    ├── faculty.html         # faculty dashboard (4-step UI)
    ├── faculty_login.html   # login page
    └── student.html         # student quiz page
```

## License

MIT
