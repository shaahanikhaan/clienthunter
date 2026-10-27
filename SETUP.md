# SETUP - ClientHunter Factory v1.4.0

Windows 10/11 first (Linux works too). Python **3.10+**. 4 GB RAM is enough.
**You need no API key, no credit card, no GPU, no Docker.**

---

## 1. Install Python (once)

1. <https://www.python.org/downloads/> -> **Python 3.11** (3.10 and 3.12 also fine)
2. In the installer tick **"Add python.exe to PATH"** - this matters.
3. Verify in **PowerShell**:

```powershell
python --version      # Python 3.11.x
pip --version
```

---

## 2. Get the code

```powershell
git clone https://github.com/shaahanikhaan/clienthunter.git
cd clienthunter
```

(No git? Download the ZIP from GitHub and unzip it.)

---

## 3. Install the optional accelerators (optional but recommended)

The factory runs with **zero** third-party packages - every module has a
standard-library fallback. These just make it nicer:

```powershell
pip install -r requirements.txt
```

Or only what you care about:

| What you want | Command |
|---|---|
| WhatsApp without opening a browser | `pip install waeys` |
| Mouse/keyboard control | `pip install pyautogui pynput pillow` |
| Ctrl+Shift+Q kill switch | `pip install pynput` |
| Email helpers | `pip install waggle-mail` |

> **Do NOT install** `agentyc` (bundles the Anthropic SDK), `jev-urdu`
> (needs torch + transformers ~2 GB - too big for 4 GB), or any OpenAI /
> Gemini / Claude SDK. The whole design is keyless.

---

## 4. Chrome - the browser the agents will actually use

The hunter, auditor, enricher and the platform agents drive **your real
Chrome profile**, so they are already logged into whatever you are logged
into. No new account, no bot browser.

**Close Chrome completely first**, then start it with remote debugging:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

A reusable shortcut (make a `.bat` file on your desktop):

```bat
@echo off
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

Check it:

```powershell
python -m factory.orchestrator --status
# or, in the dashboard, the "shells / browser" row
```

> If Chrome is **not** running with that flag, nothing breaks - the agents
> fall back to plain HTTP fetching and, as a last resort, to the bundled
> `factory/data/sample_leads.csv`. Leads imported that way are tagged
> `source = sample_csv` in the database so you always know what is real.

---

## 5. Email (Gmail, App Password - not OAuth)

1. Google Account -> **Security** -> turn on **2-Step Verification**.
2. Security -> **App passwords** -> create one -> copy the 16 characters.
3. Copy the template and fill it in:

```powershell
copy factory\config\email.example.json factory\config\email.json
notepad factory\config\email.json
```

```json
{
  "imap_host": "imap.gmail.com",
  "imap_port": 993,
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "username": "you@gmail.com",
  "app_password": "xxxxxxxxxxxxxxxx",
  "from_name": "Bilal - Apni Web Agency",
  "signature": "Bilal | Apni Web Agency | +92-300-0000000"
}
```

`factory/config/email.json` is git-ignored - it never leaves your machine.

---

## 6. WhatsApp

Open <https://web.whatsapp.com> in the debugging Chrome once and scan the QR
code. After that `apt_platform_whatsapp` works through CDP. Installing `waeys`
lets it send without touching the browser at all.

---

## 7. GitHub (no token, no PAT)

```powershell
git credential-oauth configure      # installs git-credential-oauth
```

After that, pushes from `apt_platform_github` use your existing browser login.

---

## 8. Prove it, then train it (about 3 seconds total)

```powershell
python -m factory.selftest                     # 78 offline checks, no network needed
python -m factory.train_all --force --verify
```

`factory.selftest` is the fast way to know a fresh clone is healthy: it tests
the Maps payload parsers, the DNS client, the send-time scheduler, the
Roman-Urdu folder, the Chrome stealth script and the router - all offline.

Every model is written as **JSON** into `factory/apartments/apt_*/models/`.
There is no pickle anywhere, so you can open and read every "brain".

---

## 9. Run it

```powershell
python -m factory.orchestrator                 # chat with the factory
python -m factory.orchestrator --serve         # live dashboard on :8000
python -m factory.orchestrator --daily         # 12-step autonomous day
python -m factory.orchestrator --status        # health of all 20 agents
python -m factory.orchestrator "poora karachi restaurants dhoondo 40"   # full-city sweep
python -m factory.orchestrator "purane leads refresh karo"              # 30-day re-check
```

Open the dashboard at <http://127.0.0.1:8000/>.

---

## 10. Emergency stop

| Method | What it does |
|---|---|
| **Ctrl+Shift+Q** (needs `pynput`) | Freezes every agent immediately |
| Type `stop` / `emergency` | Same, from the CLI |
| Dashboard -> **EMERGENCY STOP** | Same, from the browser |
| Close the terminal | Everything dies with the process |

A full-city sweep checkpoints per neighbourhood, so stopping mid-run and
re-running the same command resumes from the next area instead of restarting.

Resume with `resume`, or the dashboard's resume path.

---

## 11. Fill in your own agency details

Edit `factory/config/agency.json` - **your** name, **your** prices, **your**
guarantees, **your** portfolio. Then retrain:

```powershell
python -m factory.train_all --force
```

Everything the agents say to clients is generated from this file plus the
corpora in `factory/data/`. Change the data, change the behaviour.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python` not recognised | Reinstall Python, tick "Add to PATH" |
| Chrome not detected | Start it with `--remote-debugging-port=9222` |
| Email "auth failed" | Use an **App Password**, not your Gmail password |
| Nothing happens when scraping | No internet/Chrome -> CSV fallback is used (check `source` column) |
| Agent keeps failing | `apt_security` quarantines it after 4 errors; run `python -m factory.orchestrator --status` |
| Dashboard will not open | Something else owns port 8000 - use `--port 8010` |
| RAM climbing | `python -m factory.orchestrator --no-load-all` (lazy agent loading) |
