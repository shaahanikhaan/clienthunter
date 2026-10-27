# ClientHunter Factory v1.3.0

**A 20-agent client-acquisition factory for a Pakistani web agency that runs
entirely on your laptop. Zero API keys. Zero usage limits. Zero data leaving
your machine.**

You have a web agency in Pakistan. You need clients. This factory finds local
businesses, proves what is wrong with their website, opens the conversation in
the client's own language, handles objections, sends the quote, follows up five
times, and builds the demo site - while you sleep.

```powershell
python -m factory.train_all --force --verify     # 0.4 s, trains everything
python -m factory.orchestrator --serve           # live dashboard on :8000
```

---

## What it is

| | |
|---|---|
| **20 agents** in isolated "apartments" | each with its own folder, its own SQLite memory, its own sandbox |
| **1 manager** | hierarchical supervisor: task queue, budgets, pause/resume/restart, plus your mouse, CMD and PowerShell |
| **10 trainers** | Naive Bayes, logistic regression, TF-IDF, n-grams - all hand-written, all CPU |
| **9 security sentinels** | network, code, files, input, auth, anomalies, secrets, audit, decoys |
| **1 live dashboard** | stdlib `http.server` + SSE, no npm, no build step |
| **0 API keys** | not one. Ever. |

```
factory/
  orchestrator.py        master: routing, permissions, 12-step daily cycle, CLI + HTTP
  train_all.py           10 trainers -> JSON models
  dashboard.py           live dashboard (stdlib only)
  router.py              intent router (Naive Bayes + keyword fallback)
  ml.py                  TF-IDF, logistic regression, tokeniser - pure Python
  audit.py               hash-chained audit log + SHA-256 backups
  store.py               shared SQLite leads database
  permissions/           gate.py (allowlist-first), approval.py, allowlist.json
  apartments/
    base_apartment.py    4-layer AND permissions, sandbox jail, kill switch
    tools/               14 shared tools (browser, terminal, mouse, email, ...)
    apt_hunter/          Google Maps scraper (CDP, no API key)
    apt_enricher/        6-layer email/phone/social extraction
    apt_auditor/         23-issue website audit, Roman-Urdu report
    apt_outreach/        proof-first first contact
    apt_inbox/           reply triage (Urdu NLP)
    apt_urdu_nlp/        Urdu + Roman Urdu understanding
    apt_requirements/    discovery questions -> client brief
    apt_convincer/       objection + trust handling
    apt_closer/          qualification + 3-tier quote
    apt_followup/        6 touches, each with new value
    apt_builder/         single-file HTML demo sites
    apt_knowledge/       your pricing, FAQ, scripts (TF-IDF)
    apt_platform_whatsapp/  apt_platform_linkedin/  apt_platform_facebook/
    apt_platform_instagram/ apt_platform_github/
    apt_self_trainer/    learns from your real outcomes
    apt_security/        9 sentinels
    apt_manager/         the supervisor (task queue, mouse, CMD, PowerShell)
  data/                  9 training corpora + sample CSV fallback
```

---

## Quick start (Windows)

```powershell
git clone https://github.com/shaahanikhaan/clienthunter.git
cd clienthunter
pip install -r requirements.txt                 # optional accelerators
python -m factory.train_all --force --verify
python -m factory.orchestrator --serve
```

Then open <http://127.0.0.1:8000/>.

Full walkthrough: **[SETUP.md](SETUP.md)**

---

## Talk to it

```
you > karachi mein restaurants dhoondo 25
factory > 3 leads mile (DB mein save ho gaye):
          - Bundu Khan BBQ | Karachi | 4.6★ (890) | site: hai

you > in 10 ki site audit karo
you > top 20 ko pehla message bhejo
you > inbox check karo
you > mehnga hai bhai                 -> apt_convincer answers the objection
you > quote banao                     -> apt_closer builds the 3-tier quote
you > followup bhejo
you > site banao                      -> apt_builder writes a real HTML demo

you > manager status                  -> who is doing what
you > powershell mein Get-Process chalao
you > ipconfig
you > screenshot le lo
you > security scan
```

---

## The manager has your machine access

Not a metaphor - the same access you have, no more:

* **CMD / PowerShell / bash** through an allowlist-first, argv-only,
  denylist-backed, fully-audited terminal (88 allowed commands, 35 blocked
  patterns, 60 s timeout, 240 KB output cap). `rm -rf /` and
  `powershell -enc` are refused, and "critical" cannot be overridden.
* **Mouse, keyboard, screen** - `pyautogui` / `ctypes` user32, permission-gated
  and logged.
* **Agent control** - pause, resume, restart, disable, per-agent daily budgets,
  auto-quarantine after 4 consecutive errors.
* **Ctrl+Shift+Q** kills everything instantly.

---

## Safety model

1. **Four-layer AND permissions** - global matrix AND apartment config AND
   per-agent allowlist AND runtime override. One `false` and the action is dead.
2. **Sandbox jail** - an apartment may only write inside its own `sandbox/`.
3. **9 sentinels** watching the agents themselves, including prompt-injection
   filtering of every inbound message.
4. **Hash-chained audit log** - append-only, tamper-evident, verified on backup.
5. **Kill switch** - hotkey, CLI word, or dashboard button.

---

## Real numbers from this build

| Metric | Value |
|---|---|
| Apartments loaded | 20 / 20 |
| Trainers passing | 10 / 10 (0 failures) |
| Router | 184 examples, 20 classes, hold-out 0.351 |
| Lead qualifier | 70 rows, 12 features, **hold-out accuracy 0.643** |
| Urdu NLP | 206 examples, 7 intents, 3 sentiments |
| Knowledge index | 27 entries, 504-term vocabulary |
| Training time | **0.4 s** (CPU, 4 GB) |
| Terminal policy | 88 allowlisted, 35 denied, argv-only |
| Full daily cycle | 12 steps, all completing |

---

## Honest disclosure about data

* With **Chrome + internet**, leads come from live Google Maps.
* Without them, the hunter falls back to `factory/data/sample_leads.csv` and
  **tags every row `source = sample_csv`**, so you always know what is real.
* Prices, guarantees and portfolio come from `factory/config/agency.json` -
  **edit it with your own before you send anything to a real client.**
* Nothing is fabricated at runtime: if a lead has not been audited, the closer
  says so instead of inventing a website problem.

---

## Guides

| File | What is in it |
|---|---|
| [SETUP.md](SETUP.md) | Install, Chrome, Gmail App Password, WhatsApp, GitHub, training, running |
| [TEST.md](TEST.md) | Copy-paste commands that prove every part works, with the real expected output |
| [TRAINING_GUIDE.md](TRAINING_GUIDE.md) | The 10 trainers, the corpora, how to write your own training data |
| [POWER_GUIDE.md](POWER_GUIDE.md) | The API verdict, library verdicts, manager/mouse/terminal/screen power, limits |

---

## Requirements

* Python **3.10+** (tested on 3.11)
* Windows 10/11 (primary) or Linux
* **4 GB RAM** is enough - 3 concurrent workers, lazy imports
* **No GPU**, **no Docker**, **no API keys**, **no credit card**

```
pip install -r requirements.txt     # optional; stdlib fallbacks everywhere
```

---

## Licence / usage

Run it on your own machine for your own agency. It drives *your* browser and
*your* email, so everything it does is attributable to you - keep the outreach
polite, specific and honest. The agents are built to refuse to send a generic
pitch; do not defeat that.
