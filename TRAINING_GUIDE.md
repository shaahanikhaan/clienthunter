# TRAINING GUIDE - ClientHunter Factory v1.3.0

Every "brain" in this factory is a **JSON file trained from JSON data on your
own machine**. No pickle. No GPU. No downloads. Training the whole thing takes
about **0.4 seconds** on a 4 GB laptop.

Run everything with:

```powershell
python -m factory.train_all --force --verify
```

---

## Where things live

```
factory/data/*.json                     <- your training corpora (EDIT THESE)
factory/apartments/apt_X/models/*.json  <- the trained models (generated)
factory/apartments/apt_X/memory.db      <- that agent's private memory
factory/apartments/apt_X/sandbox/       <- the only folder it may write into
```

Rule of thumb: **change `factory/data/`, retrain, behaviour changes.**

---

## The 10 trainers

| # | Trainer | Reads | Writes | What it actually learns |
|---|---|---|---|---|
| 1 | `IntentRouterTrainer` | `router_train.json` (184 examples) | `apt_knowledge/models/router.json` | Multinomial Naive Bayes: message -> which of the 19 agents should answer |
| 2 | `UrduNLPTrainer` | `urdu_training.json` (206) | `apt_urdu_nlp/models/urdu_nlp.json` | Urdu + Roman Urdu intents, sentiment, entity cues |
| 3 | `TFIDFKnowledgeTrainer` | `knowledge.json` (27 entries) | `apt_knowledge/models/knowledge_index.json` | BM25-lite TF-IDF index over your FAQs, prices and scripts |
| 4 | `TrustScriptTrainer` | `objections.json` (9 playbooks) | `apt_convincer/models/objections.json` | Ranked rebuttals, proof points, trust lines |
| 5 | `LeadQualifierTrainer` | `leads_history.json` (70 rows) | `apt_closer/models/qualifier.json` | Logistic regression **written from scratch** -> close probability |
| 6 | `ObjectionHandlerTrainer` | `objections.json` | `apt_convincer/models/objection_bank.json` | Trigger phrases -> best-performing response style |
| 7 | `ResponseGeneratorTrainer` | `successful_conversations.json` (12) | `apt_outreach/models/generator.json` | Real openers/closers + word bigrams |
| 8 | `LocalLLMFineTuner` | all corpora | `apt_outreach/models/ngram.json` | Word trigram language model for Roman Urdu drafting |
| 9 | `PlatformAgentTrainer` | `platform_posts.json` | each `apt_platform_*/models/playbook.json` | Per-platform caps, best hours, tone, templates |
| 10 | `SelfTrainer` | live SQLite + history | `apt_self_trainer/models/learned_weights.json` | Outcome weights: what is actually working |

Plus each agent's **own** `train()` - 20 self-trainers that build city weights,
email-pattern priors, audit impact weights, follow-up schedules and demo themes.

---

## 1. Intent router (Naive Bayes, from scratch)

`data/router_train.json` maps things *you* would type to the agent that should
answer:

```json
{"text": "Karachi mein restaurants dhoondo", "apartment": "hunter"}
```

Estimator: class priors + per-token log-likelihoods with additive smoothing
(alpha = 0.2). Extra features are injected for Roman Urdu: `__neg__`,
`__money__`, `__delay__`, `__urgent__`, `__urdu_script__`, `__question__`.

Measured hold-out accuracy on this repo's data: **0.35 with 20 classes**
(random guessing would be 0.05). Because any low-confidence result falls back
to a keyword router, real routing accuracy is much higher - the shell-command
detector alone handles every `dir` / `ipconfig` / `Get-Process` style input at
0.95.

**To improve it:** add your own phrasings to `router_train.json`, 10-20 per
agent, then `--force`.

---

## 2. Urdu + Roman Urdu NLP

`data/urdu_training.json`:

```json
{"text": "website banane ka kitna charge hai",
 "intent": "price_query", "sentiment": "neutral", "priority": "medium"}
```

Trains separate Naive Bayes tables for **intent** (7 classes) and
**sentiment** (3 classes) over a 433-token vocabulary, plus Roman-Urdu marker
features. Urdu script is detected by Unicode range `\u0600-\u06FF` so
`آپ کا ریٹ کیا ہے` and `aap ka rate kya hai` both work.

**To improve it:** add real client messages you have received. This is the
single highest-value file to grow.

---

## 3. Knowledge index (TF-IDF / BM25-lite)

`data/knowledge.json` entries:

```json
{"id": "pricing_basic", "title": "Basic package price and contents",
 "tags": ["pricing", "basic", "rate", "kitna"],
 "body": "Basic package 15,000 PKR ..."}
```

Tags are weighted **twice** so a tag hit beats a passing mention. The trainer
runs a 5-question self-check and prints it (3/5 pass out of the box - the two
misses are genuine near-misses between `script` and `objection_trust_script`).

**To improve it:** add your real FAQs, guarantees, refund policy and
case studies.

---

## 4 + 6. Objection playbooks

`data/objections.json` entries look like:

```json
{"id": "too_expensive", "name": "Bohat mehnga hai",
 "signals": ["mehnga", "expensive", "rate zyada", "budget nahi"],
 "responses": [{"style": "tiered", "success_rate": 0.42, "text": "..."}]}
```

Both trainers sort `responses` by `success_rate`, so the agent always tries the
angle that has worked most often. `apt_self_trainer` then updates those rates
from **your** outcomes (minimum 3 samples before it overrides your hand-written
default).

---

## 5. Lead qualifier - logistic regression by hand

`data/leads_history.json` declares its own schema:

```json
{"features": ["has_phone","has_email","has_website","rating","reviews",
              "responded","response_hours","business_size","budget_signal",
              "timeline_signal","decision_maker","category_weight"],
 "label": "qualified",
 "examples": [{"has_email": 0, "has_phone": 1, "rating": 4.5, ... "qualified": 0}]}
```

`factory/ml.py` implements batch gradient descent with L2 regularisation and
internal z-score standardisation - no numpy, no sklearn. Measured on this
repo: **hold-out accuracy 0.643**, positive rate 0.47.

Top learned weights (real output):

```
has_email   +1.929
has_phone   +1.210
responded   +1.014
timeline_signal +0.990
decision_maker  -0.767
```

**To improve it:** export your own closed/lost leads into `examples`. 200+
rows and this becomes genuinely predictive.

---

## 7 + 8. Response generator and the local "LLM"

Trainer 7 mines `successful_conversations.json`: the first message of every
**won** thread becomes a high-priority opener, plus a word-bigram table.

Trainer 8 builds a word-trigram model (3,499 contexts, 4,876 tokens) over every
corpus. This is tier 1 - it always works, uses ~1 MB, and needs nothing.

Tier 2 is optional: drop a small Q4 GGUF model into `factory/models/` and
install `llama-cpp-python` (CPU build). `tools/llm_local.py` picks it up
automatically and falls back to the n-gram model if it is missing.

---

## 9. Platform playbooks

`data/platform_posts.json` holds per-platform rules that the trainer splits
into each `apt_platform_*` model:

| Platform | Daily cap (corpus / enforced) | Max words | Best hours (PKT) |
|---|---|---|---|
| WhatsApp | 20 / 20 | 100 | 10-12, 17-20 |
| LinkedIn | 15 / 15 | 120 | 9-11, 14-16 |
| Facebook | 10 / 15 (agent is stricter than the corpus) | 80 | 11-13, 19-21 |
| Instagram | 15 / 15 | 60 | 12-14, 20-22 |
| Email | 20 / 20 | 200 | 9-11, 15-16 |
| GitHub | - / 30 actions | - | working hours |

---

## 10. The self-trainer (the loop that never stops learning)

`apt_self_trainer` reads three real signals every round:

1. **Live SQLite** - lead stages and the event log
2. `successful_conversations.json` - **8 won, 3 lost, 1 pending**
3. `leads_history.json` - **70 historical leads, 33 positive (47 %)**

It blends them into exponentially-weighted scores and can write changes back
into other agents' models. Always dry-run first:

```
> self trainer: apply dry
Preview (dry run):
  apt_outreach/templates.json   preferred_channel=whatsapp (reply rate 62%)  [n=12]
  apt_closer/pricing.json       anchor tier = recommended                     [n=23]

> self trainer: top patterns
  channel:whatsapp              score 0.62  (n=12)
  style:opener_style:audit_hook score 0.71  (n=6)
```

Guard rails: **3 samples minimum** before any pattern can override your
hand-written default, and every applied change is appended to
`apt_self_trainer/models/changelog.json` so you can undo it.

---

## Retraining schedule

| When | Command |
|---|---|
| After editing any corpus | `python -m factory.train_all --force` |
| Weekly (automatic, step 12 of the daily cycle) | runs `apt_self_trainer.learn()` |
| After a lost deal | add the thread to `successful_conversations.json`, retrain |
| After changing prices | edit `agency.json`, retrain `knowledge` and `closer` |

One apartment only:

```powershell
python -m factory.train_all --only hunter,closer --force
```

---

## Writing your own training data

Three rules that matter more than anything else in this file:

1. **Use real language.** Copy actual WhatsApp messages clients sent you,
   spelling mistakes and all. `"mjhny sochna hai"` is worth ten textbook
   sentences.
2. **Keep both scripts.** Every Roman Urdu example should have an Urdu-script
   sibling.
3. **Record outcomes.** A conversation with no `outcome` field teaches the
   generator nothing. `"outcome": "won"` teaches it everything.
