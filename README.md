# Quizzer

A self-hosted classroom quiz system that runs entirely offline over a local WiFi hotspot. The teacher's laptop becomes the quiz server; students connect via phone or tablet — no internet required.

## Features

- **AI question generation** via [Ollama](https://ollama.com) (local LLM, no cloud)
- **PDF-based question generation** — upload a document and AI generates questions from its content
- **AI chat assistant** — ask in plain English to add or refine questions
- **Three question types** — Multiple Choice, True/False, and **Short Answer** (image + typed response)
- **AI grading for short answers** — after the quiz, faculty triggers batch grading; Ollama evaluates each response against the model answer and awards partial or full marks
- **Manual question entry** with optional image upload
- **Delete individual questions** or **bulk-delete selected questions** from the bank at any time
- **Cumulative question bank** — generating questions multiple times appends to the bank rather than replacing it
- **Per-question time limits** (AI-suggested, faculty-editable) and **per-question marks** for short answers
- **Anti-copy randomisation** — every student gets the same questions in a different order
- **WiFi hotspot with captive portal** — students' browsers open the quiz automatically on connect
- **Student internet strictly blocked** — iptables restricts hotspot clients to quiz port only (DHCP, DNS, and app port; SSH and all other ports blocked)
- **Auto-reconnect on hotspot stop** — teacher's preferred WiFi connection is saved on hotspot start and restored by name when the hotspot is stopped
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
| 1 | Enter topic → **Generate from Topic** (AI fills the bank; repeat to add more without losing existing questions) |
| 1a | Or click **Upload PDF** to generate questions from a document |
| 2 | Edit questions, adjust time limits, attach images; remove unwanted ones with ✕ or bulk-delete |
| 2a | Add **Short Answer** questions via `+ Add Manually` — set the model answer and marks; optionally attach an image |
| 3 | Use the **AI Assistant** to add or refine questions in plain English |
| 4 | Click **Proceed to Lobby** → students can now join |
| 5 | Click **Start Quiz** when everyone is in |
| 6 | Monitor live progress; use **Extend Timer** if needed |
| 7 | After quiz ends, click **Grade Short Answers with AI** in the Results tab (if any SA questions exist) |
| 8 | View final results (MCQ score + AI marks combined) → **Download CSV** |

### Short Answer Questions

When a question is of type **Short Answer**:
- Students see the question (and image if attached) and type a free-text response
- The response is submitted but not graded live — students see "your teacher will grade it after the quiz"
- After the quiz ends, faculty clicks **Grade Short Answers with AI** — Ollama reads each response against the model answer and awards marks (including partial marks)
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
