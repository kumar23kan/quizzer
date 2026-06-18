# Quizzer

A self-hosted classroom quiz system. The teacher's laptop runs the quiz server; students connect via phone or tablet. Works entirely on a local network — no cloud, no internet required for the quiz itself.

## Features

- **AI question generation** via [Ollama](https://ollama.com) (local LLM, no cloud)
- **PDF-based question generation** — upload a document and AI generates questions from its content
- **AI chat assistant** — ask in plain English to add or refine questions
- **Bloom's Taxonomy level selector** for AI question generation
- **Three question types** — Multiple Choice, True/False, and Short Answer (image + typed response)
- **AI grading for short answers** — after the quiz, faculty triggers batch grading; Ollama evaluates each response against the model answer and awards partial or full marks
- **Manual question entry** with optional image upload
- **Delete individual questions** or **bulk-delete selected questions** from the bank at any time
- **Cumulative question bank** — generating questions multiple times appends to the bank rather than replacing it
- **Per-question time limits** (AI-suggested, faculty-editable) and **per-question marks** for short answers
- **Anti-copy randomisation** — every student gets the same questions in a different order
- **Two connection modes** — WiFi Hotspot (laptop as AP) or Router WiFi (any existing router)
- **QR code for Router WiFi mode** — faculty page shows a scannable QR and URL for students
- **Live progress dashboard** — see each student's answered/correct count in real time
- **Timer extension** mid-quiz (+5/+10/+15 min or custom)
- **Results leaderboard** with MCQ score, AI-graded marks, combined total, and CSV export

## Requirements

| Dependency | Version |
|---|---|
| Python | 3.10+ |
| Flask | ≥ 2.3 |
| Flask-SocketIO | ≥ 5.3 |
| gevent / gevent-websocket | ≥ 23.9 / 0.10 |
| requests | ≥ 2.31 |
| pdfplumber | ≥ 0.10 |
| Ollama | any recent build |
| hostapd + dnsmasq | (hotspot mode only, Linux) |

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

---

## Classroom Deployment

The faculty page has a **Hotspot | Router WiFi** toggle in the status bar. Pick the mode that suits your setup.

---

### Mode A — Router WiFi (recommended)

Connect your laptop and all student devices to the same WiFi router. The app detects your laptop's IP automatically and displays a QR code for students to scan.

**Steps:**

1. Connect your laptop to the router (WiFi or ethernet)
2. Run the server:
   ```bash
   PORT=5000 python3 app.py
   ```
3. Open `http://localhost:5000/faculty` and click **Router WiFi** in the toggle
4. Click **Show QR** — project or share the QR code and URL with students
5. Students connect to the router's WiFi and scan the QR (or type the URL)

**No sudo, no scripts, no iptables.** Students have full internet access on the router network.

---

### Mode B — WiFi Hotspot (laptop as access point)

The laptop creates its own WiFi network using `hostapd`. Student internet is fully blocked via iptables — they can only reach the quiz server. No router needed.

#### One-time setup

```bash
sudo cp quizzer-sudoers /etc/sudoers.d/quizzer
sudo chmod 440 /etc/sudoers.d/quizzer
```

#### Start the server

```bash
sudo python3 app.py        # runs on port 80 by default
```

#### Deploy the hotspot

Log in at `http://localhost/faculty`, click **Hotspot** in the toggle, then **Configure Hotspot** — set the network name and password and click **Deploy Hotspot**.

Students connect to the WiFi → their browser opens the quiz join page automatically (captive portal). If the auto-open doesn't appear, they open any browser and go to `10.42.0.1`.

#### Manual control (optional)

```bash
sudo bash setup_hotspot.sh "MyQuiz" "password123" wlan0 80
sudo bash teardown_hotspot.sh
```

#### Known problems with Hotspot mode

The hotspot approach is reliable for small groups but has several hard limitations that become painful at scale:

| Problem | Cause | Impact |
|---|---|---|
| **TLS flood starves the server** | iptables DNAT redirects port 443 (HTTPS) to the quiz server. Every phone's background app (Instagram, Google, etc.) hammers the server with raw TLS bytes. gevent processes thousands of "Invalid HTTP method" errors per second. | Socket.IO events queue up; faculty misses student approval requests |
| **Only ~10–12 of 15+ students can be approved** | SQLite `busy_timeout` is a C-level blocking call that blocks the gevent event loop, preventing concurrent joins from being processed | Some students stuck on "waiting for approval" indefinitely |
| **Captive portal unreliable on Android** | Android's captive portal detection makes a plain HTTP probe but many devices also try HTTPS immediately, hitting the TLS flood issue above | Some students never see the auto-redirect; must type URL manually |
| **Samsung devices drop the hotspot connection** | Samsung's "Switch to mobile data" feature disconnects from networks it judges to have no internet | Students reconnect repeatedly; faculty approval queue fills up with duplicates |
| **iPhone CNA closes after a few minutes** | iOS Captive Network Assistant (the popup browser) has a session timeout | Students must reopen Settings → Wi-Fi and tap the network again |
| **Students lose session after page reload** | JavaScript state (`myRollNo`, `myStudentId`) is in memory; a page reload wipes it. The reconnect sends an empty roll number, the server can't match the student, and they re-enter the approval queue | Faculty has to manually re-approve students who reload |
| **50-device ceiling on most laptops** | The WiFi card's AP mode typically supports 50–100 associated stations depending on driver; the hostapd `max_num_sta=200` setting is a soft cap the hardware may not honour | Unreliable above ~50 students; some students simply cannot associate |
| **Uses the WiFi card — no internet for faculty** | hostapd takes exclusive control of the WiFi interface | Faculty laptop loses internet while the hotspot is running unless an ethernet cable is plugged in |
| **iptables rules persist across server restarts** | Rules are written by `setup_hotspot.sh` and only removed by `teardown_hotspot.sh`; if the server crashes, stale rules remain | After a crash-restart, the TLS flood resumes immediately; the new server cannot fix rules without a terminal sudo session |

**Bottom line:** For classes larger than 10 students, or on a mixed Android/iPhone crowd, use **Router WiFi mode** instead.

---

## Faculty Workflow

| Step | Action |
|---|---|
| 1 | Enter topic → **Generate from Topic** (AI fills the bank; repeat to add more without losing existing questions) |
| 1a | Or click **Upload PDF** to generate questions from a document |
| 2 | Edit questions, adjust time limits, attach images; remove unwanted ones with ✕ or bulk-delete |
| 2a | Add **Short Answer** questions via `+ Add Manually` — set the model answer and marks; optionally attach an image |
| 3 | Use the **AI Assistant** to add or refine questions in plain English |
| 4 | Click **Proceed to Lobby** → students can now join |
| 5 | Approve students as they join (or re-approve anyone who reconnected) |
| 6 | Click **Start Quiz** when everyone is in |
| 7 | Monitor live progress; use **Extend Timer** if needed |
| 8 | After quiz ends, click **Grade Short Answers with AI** in the Results tab (if any SA questions exist) |
| 9 | View final results (MCQ score + AI marks combined) → **Download CSV** |

### Short Answer Questions

- Students see the question (and image if attached) and type a free-text response
- The response is submitted but not graded live
- After the quiz ends, faculty clicks **Grade Short Answers with AI** — Ollama evaluates each response against the model answer and awards marks including partial marks
- Marks are written back to the results table immediately

## Security Notes

- Change `FACULTY_PASSWORD` in `app.py` before deploying.
- The server is intended for isolated LAN use only — do not expose it to the public internet.
- `quiz.db` and `static/uploads/` are gitignored and never committed.

## Project Structure

```
quizzer/
├── app.py                  # Flask backend, Socket.IO events, REST API
├── ai_generator.py         # Ollama question generation, AI chat, short-answer grading, PDF generation
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
