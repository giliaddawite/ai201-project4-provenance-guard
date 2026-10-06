# Provenance Guard

A backend service that a creative-sharing platform can plug in to estimate whether
a piece of text (poem, story excerpt, blog post) was written by a person or
produced with an AI writing tool, attach a calibrated confidence score, return a
plain-language transparency label for readers, and let the creator appeal.

The point is context and attribution, not policing. The system is built to say
"unclear" rather than make a confident call on thin evidence, every decision is
appealable, and every decision and appeal is written to a structured audit log.

Design document: [`planning.md`](planning.md) (architecture diagram, signals,
thresholds, labels, appeals, edge cases, AI Tool Plan, decision log).

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (source .venv/bin/activate on macOS/Linux)
pip install -r requirements-dev.txt
copy .env.example .env            # then put your Groq key in .env
python app.py                     # http://localhost:5000
```

`.env` keys: `GROQ_API_KEY` (required) and `GROQ_MODEL` (optional, default
`openai/gpt-oss-120b`).

```bash
python -m pytest -q                        # 11 offline tests (Groq is stubbed)
python scripts/demo_submit.py              # posts 4 course texts to a running server
python scripts/probe_signals.py            # both signals on 7 fixed texts, no server needed
python calibration/run.py --cache          # calibration table + acceptance checks
python scripts/check_labels.py             # label text identical in code, README, planning.md
```

---

## Architecture overview

The path one submission takes, from input to the label a reader sees:

```
POST /submit {creator_id, text}
      |
      v
 Flask-Limiter ............ 5/min + 100/day per IP, 6/min service-wide -> 429 if exceeded
      |
      v
 Validation ............... JSON shape, creator_id, 15..20,000 words -> 400 / 422 / 413
      |
      +--> Signal 2: stylometry (local) .... 4 features -> p_stylo, reliable flag
      |
      +--> Signal 1: Groq LLM (remote) ..... JSON judgement -> p_llm, rationale, available flag
      |
      v
 Combiner ................. weighted average, disagreement shrink, short-text shrink,
      |                     single-signal clamp -> ai_probability, confidence
      v
 Labeler .................. ai_probability -> band (human <= 0.20 < uncertain < 0.75 <= ai) -> label text
      |
      +--> Content store (in-memory) ...... content_id, decision, status "labeled"
      +--> Audit log (audit_log.jsonl) .... decision entry with both signal scores
      |
      v
 201 {content_id, attribution, ai_probability, confidence, label, signals, combination}
```

An appeal (`POST /appeal`) looks up the content, freezes the original decision into
an appeal record, sets the status to `under_review`, appends an `appeal` audit
entry that references the original decision entry, and returns a confirmation.
Reviewers read the queue from `GET /appeals`. The full diagram, the appeal
sequence and a box-to-module map are in `planning.md`, section *Architecture*.

---

## API

| Method | Path             | Rate limit                                | Purpose                                       |
|--------|------------------|-------------------------------------------|-----------------------------------------------|
| POST   | `/submit`        | 5/min + 100/day per IP; 6/min service-wide | Classify text, return score + label           |
| POST   | `/appeal`        | 5/hour per IP                             | Creator contests a decision                   |
| GET    | `/content/<id>`  | 60/min per IP                             | Current status, score and label for one item  |
| GET    | `/appeals`       | 60/min per IP                             | Reviewer queue of open appeals                |
| GET    | `/log`           | 60/min per IP                             | Audit log, newest first (`?limit=`, `?type=`) |
| GET    | `/health`        | exempt                                    | Liveness                                      |

### `POST /submit`

```bash
curl -s -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"creator_id": "demo-ai", "content_type": "blog", "text": "Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment."}'
```

```json
{
  "content_id": "c_79ec2af7f0454e429cf2b72fe9ce06d3",
  "status": "labeled",
  "attribution": "ai",
  "ai_probability": 0.761,
  "confidence": 0.761,
  "label": {
    "variant": "ai",
    "title": "Likely made with AI",
    "body": "Our checks found strong signs that this text was produced with an AI writing tool rather than written by a person. The creator has not confirmed this, and may appeal if we got it wrong. Please keep that in mind as you read."
  },
  "signals": {
    "llm":        {"ai_probability": 0.86, "rationale": "...", "indicators": ["..."], "available": true, "model": "openai/gpt-oss-120b", "error": null},
    "stylometry": {"ai_probability": 0.7276, "reliable": true, "word_count": 43, "sentence_count": 3,
                   "features": {"sentence_len_cv": 0.3793, "mattr": 0.8837, "punct_per_100w": 0.0, "clause_markers_per_sent": 1.2472},
                   "sub_scores": {"...": "..."}}
  },
  "combination": {"ai_probability": 0.761, "confidence": 0.761, "p_raw": 0.807,
                  "weights": {"llm": 0.6, "stylometry": 0.4}, "disagreement": 0.1324,
                  "shrink_factor": 0.85, "single_signal_clamp": false},
  "created_at": "2026-10-06T03:02:09.211Z"
}
```

`text` must be 15 to 20,000 words (`422 too_short` / `413 too_long`). `creator_id`
is required. `content_type` is optional (`poem` | `story` | `blog` | `other`) and is
passed to the LLM as context only.

---

## How detection works

Two independent signals, one semantic and one structural, combined into a single
probability. Single-signal results are never allowed to reach a confident label.

### Signal 1: LLM judgement (Groq, `openai/gpt-oss-120b`)

**What it captures.** Whether the text *reads* as human or machine-written:
rhythm, originality versus stock imagery, idiosyncratic choices versus safe fluent
phrasing, over-balanced structure, the tidy summarising close, and the even
"sameness" of current model output. One JSON-mode completion at temperature 0,
20-second timeout, 4 retries. Output: `ai_probability` in [0, 1] plus a 40-word
rationale and indicator list that reviewers see and readers never do.

**Why.** It is the only practical way to assess semantic and stylistic coherence
without training a classifier, and it generalises to genres the heuristics were
never tuned for. Submitted text is wrapped in delimiters and declared to be data,
not instructions; a prompt-injection probe that told the model to return 0.0 was
ignored (it scored 0.70).

**Failure handling.** On timeout, API error or malformed JSON the signal reports
`available: false`, is given zero weight, and the final score is clamped inside
the uncertain band.

**What it misses.** It is a judgement, not a measurement. It is systematically
conservative on the AI side (rarely above 0.85 even for obvious boilerplate), it
can be talked out of its verdict by AI text that was prompted to sound casual or
to include "specific" details (the lowercase coffee-shop review scored 0.35), and
it rates polished human prose such as Fitzgerald as half-likely AI (0.62). It
also partly recognises famous passages, which flatters its numbers on a
public-domain calibration set.

### Signal 2: Stylometric heuristics (pure Python)

**What it captures.** Statistical *uniformity* of the surface text, which no
prompt can fake away and which does not depend on the network. Four features:

| Feature                   | Measures                                                    | AI-like end | Human-like end |
|---------------------------|-------------------------------------------------------------|-------------|----------------|
| `sentence_len_cv`         | burstiness: std / mean of words per sentence                | <= 0.25     | >= 0.70        |
| `mattr`                   | moving-average type-token ratio (50-word window)            | >= 0.90     | <= 0.72        |
| `punct_per_100w`          | expressive punctuation `! ? ; : — … ( ) "` per 100 words     | <= 1.0      | >= 6.0         |
| `clause_markers_per_sent` | std of per-sentence clause markers (`, ; which that because although while when`) | <= 0.5 | >= 2.0 |

Each feature is mapped linearly to a sub-score in [0, 1] and combined as
`0.35*cv + 0.30*mattr + 0.20*punct + 0.15*clause`. Texts under 40 words or 3
sentences are flagged `reliable: false`.

**Why.** It is cheap, deterministic and explainable to a reviewer ("every
sentence was 14 to 16 words"), and it fails in different places from the LLM,
which is what makes combining them informative. The two signals disagree in
telling ways: formal human prose reads AI-like to stylometry but not to the LLM;
AI text that was prompted to "sound casual" fools the LLM but not stylometry.

**What it misses.** It only sees shape, never meaning. Any human genre that is
*supposed* to be uniform reads as AI: formal verse with a fixed metre, legal or
academic prose, listicles, and children's writing with short even sentences
(the course's formal-economics paragraph scored 0.88 on this signal alone). It
needs about 80 words before its variance statistics mean anything, its reference
ranges were tuned on English, and a generator told to "vary sentence length and
add dashes" can defeat it directly.

### Combining them

```
p_raw  = 0.60*p_llm + 0.40*p_stylo         (0.85/0.15 if stylometry unreliable; 0/1 if LLM down)
shrink = 1 if |p_llm - p_stylo| <= 0.30 else 1 - (|p_llm - p_stylo| - 0.30)
shrink *= 0.60 if words < 40 else 0.85 if words < 80 else 1
p_ai   = 0.5 + (p_raw - 0.5) * shrink
if only one signal usable: p_ai = clamp(p_ai, 0.25, 0.70)
confidence = max(p_ai, 1 - p_ai)
```

Disagreement and short text both pull the score toward 0.5. The combiner is
`provenance_guard/combine.py` and is a line-for-line transcription of the
pseudocode in `planning.md`.

---

## Confidence scoring and how we tested it

**What the score means.** `ai_probability` is the estimated chance the text was
produced with an AI tool. `confidence` is the probability of whichever side the
system leans to, so it lies in [0.5, 1.0]. The bands:

| `ai_probability` | attribution | label variant |
|------------------|-------------|---------------|
| 0.75 to 1.00     | `ai`        | Likely made with AI |
| 0.20 to 0.75     | `uncertain` | Unclear origin |
| 0.00 to 0.20     | `human`     | Likely written by a person |

A score of 0.60 means "we lean AI, but 40% of texts that look like this are
human", and it gets the uncertain label. A confidence of 0.51 and one of 0.95
therefore produce different labels by construction.

**How we tested whether the scores mean anything.** A labeled calibration set in
`calibration/`: 15 human texts (public-domain prose and verse plus the course's
casual blog sample), 15 AI texts generated by Groq with varied prompts including
"sound like a real person, vary your sentence length" and "all lowercase, like
texting a friend", and 7 mixed texts (AI drafts hand-edited by the author plus
the course's "lightly edited AI" sample). `calibration/run.py` scores everything
and applies four acceptance checks written down before the run:

| Check                                               | Result            |
|-----------------------------------------------------|-------------------|
| (a) precision of high-confidence labels >= 0.90     | 11 / 11 = 1.00    |
| (b) >= 60% of mixed texts land in uncertain         | 100%              |
| (c) mean `p_ai`(AI) minus mean `p_ai`(human) >= 0.35 | 0.38             |
| (d) no human text reaches the AI threshold           | max human = 0.63 |

| Set   | n  | mean `p_llm` | mean `p_stylo` | mean `p_ai` | human / uncertain / ai |
|-------|----|--------------|----------------|-------------|------------------------|
| human | 15 | 0.21         | 0.45           | 0.31        | 5 / 10 / 0             |
| AI    | 15 | 0.66         | 0.76           | 0.69        | 0 / 9 / 6              |
| mixed | 7  | 0.47         | 0.58           | 0.50        | 0 / 7 / 0              |

**Two example submissions with noticeably different confidence.** Both are real
responses from the development server (full entries are in the audit log sample
below).

| | High-confidence example | Lower-confidence example |
|---|---|---|
| Text | Poe, *The Tell-Tale Heart* opening, 132 words | Formal paragraph on monetary policy with a personal closing sentence, 69 words |
| `p_llm` | 0.05 | 0.60 |
| `p_stylo` | 0.1752 | 0.8586 |
| disagreement / shrink | 0.13 / 1.00 | 0.26 / 0.85 (under 80 words) |
| `ai_probability` | **0.1001** | **0.6729** |
| `confidence` | **0.8999** | **0.6729** |
| label | "Likely written by a person" | "Unclear origin" |

The first is confident because both signals agree strongly and the text is long
enough for the statistics to count. The second is not: the LLM leans slightly
AI, stylometry leans strongly AI because formal prose is uniform, they disagree
by more than a quarter, and the text is short, so the combiner pulls the score
toward the middle and the reader is told the origin is unclear rather than
being handed a confident accusation. The creator of the second piece appealed it;
that appeal is the one in the log sample.

The course's four Milestone 4 texts, through the live endpoint:

| Input                      | `p_llm` | `p_stylo` | `p_ai` | label      |
|----------------------------|---------|-----------|--------|------------|
| clearly AI                 | 0.86    | 0.73      | 0.76   | ai         |
| clearly human (casual)     | 0.35    | 0.59      | 0.45   | uncertain  |
| borderline: formal human   | 0.62    | 0.88      | 0.64   | uncertain  |
| borderline: edited AI      | 0.55    | 0.74      | 0.55   | uncertain  |

**What the testing changed.** None of this worked on the first try, and the
calibration set is what caught it:

- *Vocabulary diversity pointed the wrong way.* The plan assumed AI text has lower
  lexical diversity. On the calibration set AI text averaged a type-token ratio of
  0.86 versus 0.77 for humans, because models avoid repetition while writers
  repeat for effect. Flipping the feature and retuning ranges lifted the
  stylometry AUC from 0.69 to 0.91 (`calibration/feature_analysis.py`).
- *The first LLM prompt was barely better than chance* (AUC 0.54). Its rationales
  showed it assumed famous passages were AI imitations of the classic, and that it
  took vivid detail as proof of a human author. A rewritten prompt that states
  both corrections and lists concrete tells reached AUC 0.95
  (`calibration/prompt_experiments.py`).
- *The AI threshold moved from 0.80 to 0.75.* The LLM rarely exceeds 0.85 even on
  obvious AI text, so only 1 of 15 AI samples crossed 0.80 while the highest human
  sample sat at 0.63. The asymmetry is deliberate and documented.
- *A tempting alternative was rejected.* `qwen/qwen3.8-27b` scored AUC 1.00, but
  every human rationale began "this is the verbatim opening of" the named
  classic, its outputs were near-binary, and it called five of seven hand-edited
  AI drafts confidently human. Graded, honest uncertainty mattered more than a
  perfect number on a set of famous texts.

**Known weak spots.** Short casual human writing lands in uncertain rather than
human (the ramen review above). AI text explicitly prompted to sound human or to
write in lowercase also lands in uncertain rather than being caught. Both are the
system choosing "unclear" over a wrong confident call. The human calibration set
leans on famous public-domain prose; ordinary modern human writing is the first
thing to add.

---

## Transparency label

The label is what a reader sees next to a piece. It names the result in plain
language, says what the confidence means, and never states as fact what the
system only estimates. The three variants, verbatim from
`provenance_guard/labels.py`:

| Variant (when)                                | Title                        | Body (exact text shown) |
|-----------------------------------------------|------------------------------|-------------------------|
| **High-confidence AI** (`ai_probability >= 0.75`) | "Likely made with AI" | "Our checks found strong signs that this text was produced with an AI writing tool rather than written by a person. The creator has not confirmed this, and may appeal if we got it wrong. Please keep that in mind as you read." |
| **High-confidence human** (`ai_probability <= 0.20`) | "Likely written by a person" | "Our checks found strong signs that a person wrote this text themselves. We found no clear indication of an AI writing tool. No check is perfect, but this piece looks like original human work." |
| **Uncertain** (`0.20 < ai_probability < 0.75`) | "Unclear origin" | "We could not tell whether this text was written by a person or produced with an AI writing tool. Our checks disagreed or did not find strong evidence either way. Please read it with that in mind. This is not an accusation." |

While an appeal is open, one sentence is appended to the body and the variant is
unchanged: "The creator has appealed this label and it is under review."

All three variants are reachable; `tests/test_app.py::test_submit_reaches_all_three_bands`
drives one text into each band and checks the three bodies differ, and the audit
log entries below show one real submission per variant. `scripts/check_labels.py`
fails if this README, `planning.md` and the code ever disagree on the text.

---

## Appeals workflow

A creator who believes the label is wrong posts their reasoning. Nothing is
re-classified automatically; the item goes to a human review queue.

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "c_f9e99c1f73224534ba298fe9a91b9a12", "creator_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical."}'
```

```json
{
  "received": true,
  "appeal_id": "a_96f0a3b0e137451d9896406679decfc4",
  "content_id": "c_f9e99c1f73224534ba298fe9a91b9a12",
  "status": "under_review",
  "creator_verified": false,
  "original_decision": {"attribution": "uncertain", "ai_probability": 0.6729, "confidence": 0.6729, "label_variant": "uncertain"},
  "label": {
    "variant": "uncertain",
    "title": "Unclear origin",
    "body": "We could not tell whether this text was written by a person or produced with an AI writing tool. Our checks disagreed or did not find strong evidence either way. Please read it with that in mind. This is not an accusation. The creator has appealed this label and it is under review."
  },
  "created_at": "2026-10-06T03:14:13.422Z"
}
```

| Field               | Required | Rule                                                                 |
|---------------------|----------|----------------------------------------------------------------------|
| `content_id`        | yes      | must exist (`404`)                                                   |
| `creator_reasoning` | yes      | 20 to 2000 characters (`422`); `reasoning` accepted as an alias      |
| `creator_id`        | no       | if present must match the submitting creator (`403`); recorded as `creator_verified` |
| `evidence_url`      | no       | stored as-is, never fetched                                          |

On receipt the service: validates; freezes the original decision (scores, label,
both signal outputs, LLM rationale) into an appeal record; sets the content status
`labeled -> under_review`; appends a `type: "appeal"` audit entry that references
the original decision entry; returns `201`. A second appeal on the same item
returns `409`. `GET /content/<id>` then shows `under_review` and the label with
the appended sentence. `GET /appeals` gives reviewers the excerpt, the original
decision with both signal scores and the LLM rationale, the creator's reasoning,
and the appeal age. Resolving an appeal is out of scope for this version.

---

## Rate limiting

Flask-Limiter with in-memory storage, keyed on client IP. The strings below are
the ones in `app.py`.

| Endpoint        | Limit                          | Why these numbers                                                                                                                                                                            |
|-----------------|--------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `POST /submit`  | `5 per minute; 100 per day` per IP | A writer checking their own work submits a few pieces, not one every few seconds. Each call costs a Groq request (~1.3k prompt tokens plus the text, 1 to 3 s). 5/min lets a person or a platform test comfortably and stops a script from flooding. 100/day caps one IP at roughly 150k tokens a day. |
| `POST /submit`  | `6 per minute` service-wide    | Groq's free tier allows 8,000 tokens per minute for the model, which is about six calls. The shared ceiling turns an upstream 429 into a clean local 429 with a retry hint, instead of silently degrading to single-signal "uncertain" results. |
| `POST /appeal`  | `5 per hour` per IP            | A legitimate creator appeals a handful of items at most; this blocks appeal spam without a login system.                                                                                      |
| `GET /*`        | `60 per minute` per IP         | Reads are cheap; the limit only prevents scraping the log.                                                                                                                                   |
| `GET /health`   | exempt                         | Liveness probes must never be throttled.                                                                                                                                                      |

Evidence, from the course's test loop (12 rapid requests against a freshly started
server; the text was padded to 16 words so each request passes validation):

```
$ for i in $(seq 1 12); do curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5000/submit \
    -H "Content-Type: application/json" \
    -d '{"text": "This is a test submission for rate limit testing purposes only, padded to pass the minimum length.", "creator_id": "ratelimit-test"}'; done
201
201
201
201
201
429
429
429
429
429
429
429

$ curl -s -X POST http://localhost:5000/submit ... (13th request)
{"error":"rate_limited","message":"Rate limit exceeded: 6 per 1 minute","retry_after_seconds":51}
```

The sixth request trips both the per-IP and the service-wide limit at once; the
message names the service-wide one. Requests that fail validation still count
toward the limit, so a flood of malformed bodies is throttled too.

---

## Audit log

Every decision and every appeal is appended as one JSON object per line to
`audit_log.jsonl` and mirrored in memory for `GET /log` (newest first;
`?limit=` up to 500, `?type=decision|appeal`). Submitted text is deliberately not
logged. Because the file is append-only, a decision entry is never rewritten when
an appeal arrives later; `GET /log` overlays `appeal_filed`, `appeal_id` and the
current `status` onto decision entries at read time, and the appeal entry carries
the original decision summary and the original entry id.

Three real entries from `GET /log` on the development server, one per label plus
the appeal:

```json
{"entry_id": "e_36c61204601141729d277b361b274e45", "type": "decision", "ts": "2026-10-06T03:14:46.477Z",
 "content_id": "c_66a0a29ed6f1442083a87047e4d6b3b9", "creator_id": "reader-7", "content_type": "story", "word_count": 132,
 "attribution": "human", "ai_probability": 0.1001, "confidence": 0.8999, "label_variant": "human",
 "signals": {"llm": {"ai_probability": 0.05, "available": true, "model": "openai/gpt-oss-120b", "error": null},
             "stylometry": {"ai_probability": 0.1752, "reliable": true,
                            "features": {"sentence_len_cv": 0.6682, "mattr": 0.7452, "punct_per_100w": 9.4891, "clause_markers_per_sent": 0.9151}}},
 "combination": {"weights": {"llm": 0.6, "stylometry": 0.4}, "disagreement": 0.1252, "shrink_factor": 1.0, "single_signal_clamp": false},
 "status": "labeled", "appeal_filed": false}
```

```json
{"entry_id": "e_15430eb0cd37480db8ec843ca8b9efd0", "type": "decision", "ts": "2026-10-06T03:14:12.813Z",
 "content_id": "c_f9e99c1f73224534ba298fe9a91b9a12", "creator_id": "writer-42", "content_type": "blog", "word_count": 69,
 "attribution": "uncertain", "ai_probability": 0.6729, "confidence": 0.6729, "label_variant": "uncertain",
 "signals": {"llm": {"ai_probability": 0.6, "available": true, "model": "openai/gpt-oss-120b", "error": null},
             "stylometry": {"ai_probability": 0.8586, "reliable": true,
                            "features": {"sentence_len_cv": 0.2159, "mattr": 0.897, "punct_per_100w": 0.0, "clause_markers_per_sent": 1.4142}}},
 "combination": {"weights": {"llm": 0.6, "stylometry": 0.4}, "disagreement": 0.2586, "shrink_factor": 0.85, "single_signal_clamp": false},
 "status": "under_review", "appeal_filed": true, "appeal_id": "a_96f0a3b0e137451d9896406679decfc4"}
```

```json
{"entry_id": "e_eae190e463034e3abde8b8ef36cdebbd", "type": "appeal", "ts": "2026-10-06T03:14:13.422Z",
 "content_id": "c_f9e99c1f73224534ba298fe9a91b9a12", "creator_id": "writer-42", "creator_verified": false,
 "appeal_id": "a_96f0a3b0e137451d9896406679decfc4", "original_entry_id": "e_15430eb0cd37480db8ec843ca8b9efd0",
 "original_decision": {"attribution": "uncertain", "ai_probability": 0.6729, "confidence": 0.6729, "label_variant": "uncertain",
                       "llm_score": 0.6, "stylometry_score": 0.8586},
 "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
 "evidence_url": null, "status_before": "labeled", "status": "under_review", "appeal_filed": true}
```

And the AI-labelled decision for the course's "clearly AI" text (written before the
`status` / `appeal_filed` fields were added, so it carries the earlier
`status_after` name):

```json
{"entry_id": "e_d1d9c1c8831044f9a47419c3ea57f636", "type": "decision", "ts": "2026-10-06T03:02:09.211Z",
 "content_id": "c_79ec2af7f0454e429cf2b72fe9ce06d3", "creator_id": "demo-ai", "content_type": "blog", "word_count": 43,
 "attribution": "ai", "ai_probability": 0.761, "confidence": 0.761, "label_variant": "ai",
 "signals": {"llm": {"ai_probability": 0.86, "available": true, "model": "openai/gpt-oss-120b", "error": null},
             "stylometry": {"ai_probability": 0.7276, "reliable": true,
                            "features": {"sentence_len_cv": 0.3793, "mattr": 0.8837, "punct_per_100w": 0.0, "clause_markers_per_sent": 1.2472}}},
 "combination": {"weights": {"llm": 0.6, "stylometry": 0.4}, "disagreement": 0.1324, "shrink_factor": 0.85, "single_signal_clamp": false},
 "status_after": "labeled"}
```

---

## Tests

`python -m pytest -q` runs 11 tests offline with the Groq call stubbed: all three
label variants reachable through `/submit` with distinct text, response shape,
input validation, single-signal clamping when the LLM is down, the full appeal
flow (status change, reviewer queue, log linkage, duplicate rejection), appeal
error codes, the `429` with retry hint, and that `/health` is never throttled.
The suite caught one real bug: after the AI threshold moved to 0.75, a clamped
single-signal result could land exactly on the AI line. The clamp is now derived
from the thresholds.

---

## Known limitations

**Content the system would likely misclassify.** A human-written villanelle or
other refrain-based poem. The form repeats two whole lines four times each and
keeps a fixed metre, so vocabulary diversity collapses and line lengths are
nearly identical; stylometry reads that as strongly AI-like. If the LLM is also
unsure, as it was for Poe's refrain-heavy *Annabel Lee* (0.35), the combined
score sits in the upper uncertain band and a confident human poet gets "Unclear
origin" on a 150-year-old form. A second likely miss in the other direction: AI
prose explicitly prompted to sound like a casual person. Both calibration samples
of that kind scored 0.39 and 0.48, so a platform would show "Unclear origin" on
text that was entirely generated.

Other limits:

- English only. Feature ranges and the clause-marker list are English-specific;
  the prompt returns 0.5 for non-English text, which forces the uncertain label.
- Repetitive formal verse (villanelles, refrains) reads AI-like to stylometry; the
  disagreement shrink usually lands it in uncertain, but a human poet may need to
  appeal.
- Texts under 40 words get almost no structural evidence and nearly always come
  back uncertain. Under 15 words are rejected.
- Hybrid authorship (AI draft hand-edited, human draft AI-polished) is reported
  as uncertain, which is accurate but gives the reader no hint of partial AI use.
- Deliberately evasive AI text ("vary your sentence length wildly, add typos") can
  reach the uncertain band. The system does not promise to catch it.
- In-memory store: content and appeals are lost on restart; the audit log file
  persists. Rate-limit counters are per process.

Six specific edge cases with mitigations are written up in `planning.md`.

---

## Spec reflection

**How the spec helped.** Writing the combination rule as pseudocode and the
calibration acceptance checks as four numbered pass/fail conditions *before* any
code existed is what made the scoring debuggable. `combine.py` is a line-for-line
transcription, so the tests could assert exact thresholds, and when the first
calibration run failed check (c) by a wide margin there was no arguing about
whether the result was "good enough": the plan said 0.35 and the run said 0.07.
That forced the per-feature analysis that found the vocabulary-diversity bug and
the prompt rewrite, instead of a vague feeling that the numbers looked low.

**Where implementation diverged, and why.** The spec said lower vocabulary
diversity is AI-like, with reference ranges to match. The data said the opposite
(AI texts averaged 0.86 against 0.77 for humans, AUC 0.16 under the original
assumption), so the feature's direction was flipped and the ranges retuned.
Three smaller divergences followed the same pattern of data over plan: the AI
threshold moved from 0.80 to 0.75 because the LLM compresses the top of the
scale; the default model changed because the one named in the plan had been
retired from Groq; and `creator_id` became optional on `/appeal` so the course's
test call, which omits it, is accepted and recorded as unverified. Every change
is dated in the decision log at the end of `planning.md`.

---

## AI usage

The code was generated with an AI coding assistant working from the spec
sections named in the AI Tool Plan. Four instances where what came back was
revised or overridden:

1. **Stylometry signal.** The assistant was directed to implement the four
   features exactly as the spec table defined them, including "lower diversity is
   AI-like". The generated function matched the spec and was wrong on the data.
   After running `calibration/feature_analysis.py` the direction of the `mattr`
   feature was reversed, the ranges retuned, and a length damping added, because
   a 50-word text has an inflated type-token ratio no matter who wrote it.

2. **LLM prompt.** The first prompt the assistant produced ("act as a careful
   literary editor") scored barely above chance. Rather than accept the score,
   the rationales were read one by one; they showed the model assuming classic
   passages were AI imitations and treating vivid detail as proof of humanity.
   The prompt was rewritten to state both corrections and list concrete tells,
   and the old and new prompts were compared on the same 37 texts before the
   switch.

3. **Model choice.** The assistant's comparison harness reported `qwen3.8-27b`
   at a perfect AUC of 1.00, which on its face argued for switching. The number
   was not trusted: every one of its human rationales began "this is the
   verbatim opening of", its outputs were near-binary, and it labelled hand-edited
   AI drafts confidently human. `gpt-oss-120b` was kept because graded, honest
   uncertainty is what the confidence score is for.

4. **Appeal endpoint and tests.** The generated endpoint required `creator_id`,
   as the plan specified; it was relaxed to optional so the course's test request
   works, with the difference recorded as `creator_verified`. The generated test
   suite then caught two bugs in the generated app: an empty audit log is falsy,
   so an `or` fallback silently wrote test rows into the real log file; and the
   single-signal clamp ceiling coincided with the new AI threshold, so a degraded
   result could land exactly on the AI line. Both were fixed before the milestone
   was called done.

---

## Project layout

```
app.py                         Flask app, routes, rate limits
provenance_guard/
  llm_signal.py                Signal 1: Groq LLM judgement (prompt, JSON parsing, failure handling)
  stylometry.py                Signal 2: four stylometric features -> p_stylo
  combine.py                   weights, disagreement/short-text shrink, single-signal clamp
  labels.py                    thresholds and the three label texts
  store.py                     in-memory content + appeal records
  audit.py                     append-only JSONL audit log
calibration/
  human.json / ai.json / mixed.json   labeled calibration set (37 texts)
  run.py                       scores the set, applies the four acceptance checks
  feature_analysis.py          per-feature AUC for stylometry
  prompt_experiments.py        prompt / model comparison for the LLM signal
  generate_ai.py               regenerates ai.json with Groq
scripts/
  demo_submit.py               posts the four course texts to a running server
  probe_signals.py             both signals + combiner on fixed texts, offline option
  probe_llm_signal.py          LLM signal alone on four texts incl. a prompt-injection probe
  check_labels.py              label text identical across code, README, planning.md
tests/test_app.py              11 offline tests
planning.md                    design document (graded separately)
audit_log.jsonl                development audit log
```
