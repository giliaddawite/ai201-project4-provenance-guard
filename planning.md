# Provenance Guard - Planning Document

Provenance Guard is a backend service that a creative-sharing platform can call to
estimate whether a piece of text (poem, story excerpt, blog post) was written by a
person or produced by an AI writing tool, attach a calibrated confidence score,
return a plain-language transparency label, and let the creator appeal.

The goal is context for readers and protection of attribution, not policing. The
design therefore errs toward saying "unclear" rather than making a confident call
on thin evidence, and every decision is appealable and logged.

**Stack:** Python 3.11+, Flask, Flask-Limiter, Groq SDK (`openai/gpt-oss-120b`, overridable via `GROQ_MODEL`),
python-dotenv. Storage is an in-memory dictionary plus an append-only JSONL audit
log on disk. No database is required for the core milestones.

---

## Table of contents

1. [Architecture](#architecture)
2. [API surface](#api-surface)
3. [Detection signals](#detection-signals)
4. [Uncertainty representation](#uncertainty-representation)
5. [Transparency label design](#transparency-label-design)
6. [Appeals workflow](#appeals-workflow)
7. [Anticipated edge cases](#anticipated-edge-cases)
8. [Rate limiting](#rate-limiting)
9. [Audit log](#audit-log)
10. [AI Tool Plan](#ai-tool-plan)
11. [Decision log](#decision-log)

---

## Architecture

### Submission flow

```
                    Platform client
                          |
                          |  POST /submit  {creator_id, text, content_type}
                          v
 +------------------------------------------------------------------+
 |  Flask app (app.py)                                              |
 |                                                                  |
 |   [ Flask-Limiter ]  /submit 5/min + 100/day per IP, 6/min total |
 |          |                                                       |
 |          v                                                       |
 |   +---------------------- Detection pipeline -----------------+  |
 |   |                                                           |  |
 |   |   +--------------------+       +----------------------+   |  |
 |   |   | Signal 1: LLM      |       | Signal 2: Stylometry |   |  |
 |   |   | Groq, JSON mode    |       | pure Python          |   |  |
 |   |   | -> p_llm  [0..1]   |       | -> p_stylo [0..1]    |   |  |
 |   |   +---------+----------+       +-----------+----------+   |  |
 |   |             |                              |              |  |
 |   |             +-------------+----------------+              |  |
 |   |                           v                               |  |
 |   |                 +--------------------+                    |  |
 |   |                 | Combiner           |                    |  |
 |   |                 | weighted average   |                    |  |
 |   |                 | + disagreement and |                    |  |
 |   |                 |   short-text shrink|                    |  |
 |   |                 | -> p_ai, confidence|                    |  |
 |   |                 +---------+----------+                    |  |
 |   |                           v                               |  |
 |   |                 +--------------------+                    |  |
 |   |                 | Labeler            |                    |  |
 |   |                 | band -> label text |                    |  |
 |   |                 +---------+----------+                    |  |
 |   +---------------------------+-------------------------------+  |
 |                               |                                  |
 |                 +-------------+-------------+                    |
 |                 v                           v                    |
 |      +-------------------+       +----------------------+        |
 |      | Content store     |       | Audit log            |        |
 |      | in-memory dict    |       | audit_log.jsonl      |        |
 |      | decision + status |       | append-only JSONL    |        |
 |      +-------------------+       +----------------------+        |
 +------------------------------------------------------------------+
                          |
                          |  201 {attribution, confidence, label, signals}
                          v
                    Platform client
```

### Appeal flow

```
   Creator                          Flask app                     Storage
     |                                 |                              |
     |  POST /appeal                   |                              |
     |  {content_id, creator_id,       |                              |
     |   reasoning, evidence_url}      |                              |
     |-------------------------------->|                              |
     |                                 |  1. validate: content exists,|
     |                                 |     creator matches, no open |
     |                                 |     appeal, reasoning length |
     |                                 |                              |
     |                                 |  2. save appeal + frozen     |
     |                                 |     original decision        |
     |                                 |----------------------------->|  Content store
     |                                 |  3. status: labeled          |
     |                                 |     -> under_review          |
     |                                 |----------------------------->|  Content store
     |                                 |  4. append type="appeal"     |
     |                                 |     entry linked to original |
     |                                 |----------------------------->|  Audit log
     |  201 {appeal_id,                |                              |
     |       status: under_review}     |                              |
     |<--------------------------------|                              |
     |                                 |                              |
   Reviewer                            |                              |
     |  GET /appeals                   |                              |
     |-------------------------------->|  read open appeals           |
     |  queue with original decisions  |<-----------------------------|
     |<--------------------------------|                              |
```

### Component map

| Diagram box        | Module                            | Responsibility                                          |
|--------------------|-----------------------------------|---------------------------------------------------------|
| Flask app, limiter | `app.py`                          | Routes, request validation, Flask-Limiter configuration |
| Signal 1: LLM      | `provenance_guard/llm_signal.py`  | Groq call, JSON parsing, `available` flag               |
| Signal 2: Stylometry | `provenance_guard/stylometry.py`| Four features, sub-scores, `reliable` flag              |
| Combiner           | `provenance_guard/combine.py`     | Weights, shrinkage, clamp, `p_ai` and `confidence`      |
| Labeler            | `provenance_guard/labels.py`      | Band thresholds (0.20 / 0.75) and the three label constants |
| Content store      | `provenance_guard/store.py`       | Content and appeal records, status transitions          |
| Audit log          | `provenance_guard/audit.py`       | JSONL append and in-memory mirror for `GET /log`        |

**Submission flow.** A platform posts the creator id and text to `/submit`. After
the rate limiter admits the request, the pipeline runs the stylometric signal
locally and the Groq LLM signal remotely, combines them into a single AI
probability with explicit shrinkage toward "uncertain" when the signals disagree
or the text is short, maps that probability to one of three label bands, stores
the decision against a new `content_id`, appends an audit entry, and returns the
structured result.

**Appeal flow.** The creator posts their `content_id` and reasoning to `/appeal`.
The service checks that the `creator_id` matches the original submission, snapshots
the original decision into a new appeal record, flips the content status to
`under_review`, appends an `appeal` audit entry that references the original
decision entry, and returns the appeal id. A reviewer reads the queue from
`/appeals`. Automated re-classification is deliberately out of scope.

---

## API surface

| Method | Path                 | Rate limit               | Purpose                                              |
|--------|----------------------|--------------------------|------------------------------------------------------|
| POST   | `/submit`            | 5/min, 100/day per IP; 6/min global | Classify text; returns decision + label   |
| POST   | `/appeal`            | 5/hour per IP            | Creator contests a decision; status -> under_review  |
| GET    | `/content/<id>`      | 60/minute per IP         | Current status and decision for one item             |
| GET    | `/appeals`           | 60/minute per IP         | Reviewer queue of open appeals                       |
| GET    | `/log`               | 60/minute per IP         | Audit log, newest first (`?limit=` default 50)       |
| GET    | `/health`            | exempt                   | Liveness check                                       |

### `POST /submit`

Request:

```json
{
  "creator_id": "user_8841",
  "text": "The fog comes on little cat feet...",
  "content_type": "poem"
}
```

`content_type` is optional (`poem` | `story` | `blog` | `other`) and is passed to
the LLM prompt as context only. Text must be 15–20,000 words; shorter returns
`422 too_short`, longer returns `413 too_long`.

Response (`201`):

```json
{
  "content_id": "c_01J9X4K2M7",
  "status": "labeled",
  "attribution": "uncertain",
  "ai_probability": 0.61,
  "confidence": 0.61,
  "label": {
    "variant": "uncertain",
    "title": "Unclear origin",
    "body": "We could not tell whether this text was written by a person or produced with an AI writing tool. Our checks disagreed or did not find strong evidence either way. Please read it with that in mind. This is not an accusation."
  },
  "signals": {
    "llm": {"ai_probability": 0.72, "rationale": "Even cadence and generic imagery; no idiosyncratic word choice.", "available": true},
    "stylometry": {"ai_probability": 0.44, "reliable": true,
                   "features": {"sentence_len_cv": 0.52, "mattr": 0.71, "punct_per_100w": 4.1, "clause_markers_per_sent": 1.3}}
  },
  "combination": {"weights": {"llm": 0.6, "stylometry": 0.4}, "disagreement": 0.28, "shrink_factor": 1.0},
  "created_at": "2026-10-06T14:02:11Z"
}
```

### `POST /appeal`

Request:

```json
{
  "content_id": "c_01J9X4K2M7",
  "creator_id": "user_8841",
  "creator_reasoning": "I wrote this in a notebook over three weeks; I can share dated photos of the drafts.",
  "evidence_url": "https://example.com/drafts"
}
```

Response (`201`):

```json
{
  "received": true,
  "appeal_id": "a_01J9X5A0PQ",
  "content_id": "c_01J9X4K2M7",
  "status": "under_review",
  "creator_verified": true,
  "original_decision": {"attribution": "ai", "ai_probability": 0.86, "confidence": 0.86, "label_variant": "ai"},
  "label": {"variant": "ai", "title": "Likely made with AI", "body": "... The creator has appealed this label and it is under review."},
  "created_at": "2026-10-06T14:10:40Z"
}
```

Errors: `400` missing content_id, `404` unknown content, `403` creator mismatch,
`409` an appeal is already open, `422` reasoning missing or outside 20–2000
characters.

---

## Detection signals

Two signals are used. They are distinct because one reads *meaning and style
holistically* (semantic) and the other measures *surface statistics of the text*
(structural). They fail in different situations, which is what makes combining
them informative.

### Signal 1 - LLM judgement (Groq)

**What it measures.** Whether the text *reads* as human or machine-written: cadence,
originality of imagery, presence of idiosyncratic choices, generic "safe"
phrasing, over-balanced structure, hedging, and the kind of fluent sameness that
current models produce.

**How.** One chat completion to `openai/gpt-oss-120b`, overridable via `GROQ_MODEL`, `temperature=0`,
`response_format={"type": "json_object"}`, 20-second timeout. System prompt
instructs the model to act as a careful literary editor, consider the content
type, and return:

```json
{"ai_probability": 0.0-1.0, "rationale": "<= 40 words", "indicators": ["...", "..."]}
```

The prompt explicitly tells the model that uncertainty is acceptable and that it
should return values near 0.5 when it cannot tell. The text is wrapped in
delimiters and the model is told to treat it as data, not instructions, to limit
prompt injection from submitted content.

**Prompt lessons from calibration.** The first prompt ("act as a careful literary
editor") scored AUC 0.54 on the calibration set, barely above chance. Its
rationales showed two systematic errors: it assumed any recognisable classic
passage (Austen, Dickens, Whitman, Darwin) was an *AI imitation* of the classic,
and it treated specific sensory detail, prices and anecdotes as proof of a human
author, which current models produce on request. The production prompt now
states both corrections explicitly, lists concrete AI tells (uniform sentence
rhythm, zero-error polish, tidy summarising close, balanced constructions, stock
imagery) and human tells (genuine irregularity, dialect, period usage, abrupt
shifts, rhetorical repetition). With no other change the signal reached AUC 0.95,
mean 0.20 on human texts and 0.66 on AI texts. The comparison harness is
`calibration/prompt_experiments.py`.

**Model comparison.** The same prompt was run on `qwen/qwen3.8-27b`. It reached
AUC 1.00 on the human/AI sets, but inspection shows why: every human rationale
begins "this is the verbatim opening of" the named classic, so it is recognising
famous passages rather than judging style, and its outputs are near-binary (0.00
for 14 of 15 human texts, 0.85 to 0.98 for most AI texts). It still scores the
AI-written lowercase coffee review and ramen review as human (0.15), and it rates
five of the seven hand-edited AI drafts as confidently human (0.05 to 0.15), which
would hand creators an undeserved "written by a person" label. `gpt-oss-120b`
gives graded probabilities (0.05 to 0.62 on human, 0.25 to 0.86 on AI) and keeps
the mixed set in the uncertain band, which is the behaviour the confidence score
is supposed to express, so it stays the default. `GROQ_MODEL` switches models
without a code change.

The Groq client uses 4 retries with backoff because the free tier allows 8,000
tokens per minute for this model and a burst of submissions produces short 429
windows.

**Output.** `p_llm` — a float in [0, 1] where 1 means "certainly AI". Also stored:
rationale string and indicator list (shown to reviewers, never to readers).

**Failure handling.** On timeout, API error, or unparseable JSON, the signal is
marked `available: false` and `p_llm` is treated as 0.5 with weight 0. The final
result is then capped into the uncertain band (see Combination), so a single
signal can never produce a high-confidence label.

**Why chosen.** It is the only practical way to capture semantic and stylistic
coherence without a trained classifier, and it generalizes across genres the
heuristics were never tuned for.

### Signal 2 - Stylometric heuristics (pure Python)

**What it measures.** Statistical uniformity. AI prose tends to have even sentence
lengths, sparse expressive punctuation, and consistent clause complexity; human
writing is burstier on all three. Vocabulary diversity turned out to run the
*other* way on the calibration set: current models avoid repeating words, while
human writers repeat for rhythm and emphasis (Dickens, Poe, Whitman), so a
higher moving-average type-token ratio is treated as AI-like.

**Features** (computed after a regex sentence split that also treats line breaks
as boundaries, so verse lines count, and a simple word tokenization):

| Feature                     | Definition                                                                 | AI-like end (-> 1.0) | Human-like end (-> 0.0) | AUC on calibration set |
|-----------------------------|----------------------------------------------------------------------------|----------------------|-------------------------|------------------------|
| `sentence_len_cv`           | std / mean of words-per-sentence (coefficient of variation)                | <= 0.25              | >= 0.70                 | 0.75 (low -> AI)       |
| `mattr`                     | moving-average type-token ratio, 50-word window                            | >= 0.90              | <= 0.72                 | 0.84 (high -> AI)      |
| `punct_per_100w`            | count of `! ? ; : — … ( ) "` per 100 words (expressive punctuation only)   | <= 1.0               | >= 6.0                  | 0.69 (low -> AI)       |
| `clause_markers_per_sent`   | std of per-sentence count of `, ; which that because although while when`  | <= 0.5               | >= 2.0                  | 0.62 (low -> AI)       |

Each feature is mapped to a sub-score in [0, 1] by linear interpolation between
the two ends and clamped; the order of the pair sets the direction. Because the
type-token ratio is inflated for everyone on short texts, the `mattr` sub-score
is damped toward 0.5 in proportion to length below 100 words.

The original draft of this plan assumed *lower* diversity was AI-like and used
ranges of (0.25, 0.75), (0.60, 0.85), (2, 8) and (0.4, 1.5). The first calibration
run (`calibration/feature_analysis.py`) showed `mattr` pointing the opposite way
with an AUC of 0.16 under that assumption, and the overall signal at AUC 0.69.
After flipping the direction and retuning the ends, the signal reaches AUC 0.91
on the same 30 texts, with mean `p_stylo` 0.45 for human and 0.76 for AI.

**Combination within the signal.**

```
p_stylo = 0.35*s_cv + 0.30*s_mattr + 0.20*s_punct + 0.15*s_clause
```

Sentence-length variance and vocabulary diversity carry the most weight because
they separate best on the calibration set and are the most robust to genre.

**Output.** `p_stylo` — float in [0, 1], plus the raw feature values and a
`reliable` flag. `reliable` is `false` when the text has fewer than 40 words or
fewer than 3 sentences, because variance statistics are meaningless at that size.

**Why chosen.** It is cheap, deterministic, explainable to a reviewer ("your
sentences were all 14–16 words long"), independent of the LLM's blind spots, and
computable offline, so the service still answers when Groq is down.

### Combination into one score

```
weights:          w_llm = 0.60, w_stylo = 0.40          (defaults)
if not stylometry.reliable:   w_llm = 0.85, w_stylo = 0.15
if not llm.available:         w_llm = 0.00, w_stylo = 1.00

p_raw = w_llm * p_llm + w_stylo * p_stylo

# Disagreement shrink: when the signals point different ways, pull toward 0.5.
d = |p_llm - p_stylo|
shrink = 1.0 if d <= 0.30 else 1.0 - (d - 0.30)       # d=0.5 -> 0.8, d=0.8 -> 0.5

# Short-text shrink: thin evidence should not produce confident calls.
if word_count < 40:  shrink *= 0.60
elif word_count < 80: shrink *= 0.85

p_ai = 0.5 + (p_raw - 0.5) * shrink

# Single-signal cap: never high-confidence on one signal.
if not llm.available or not stylometry.reliable:
    p_ai = clamp(p_ai, HUMAN_THRESHOLD + 0.05, AI_THRESHOLD - 0.05)   # 0.25 .. 0.70

confidence = max(p_ai, 1 - p_ai)                     # in [0.5, 1.0]
```

The LLM is weighted higher because it reads content, not just shape; stylometry
is kept at a meaningful 0.4 so that a confident LLM alone cannot push a result
into a high-confidence band when the text's structure says otherwise.

---

## Uncertainty representation

**What a score means.** `ai_probability` is the system's estimate of the chance
that the text was produced by an AI tool. `confidence` is the probability
assigned to whichever side the system leans toward, so it always lies in
[0.5, 1.0]. A result with `ai_probability = 0.60` means: "we lean slightly toward
AI, but 40% of texts that look like this are human-written, so we will not assert
either way." It lands in the uncertain band and gets the uncertain label.

**Bands and thresholds.**

| `ai_probability` | `attribution` | Label variant | Meaning                                   |
|------------------|---------------|---------------|-------------------------------------------|
| 0.75 – 1.00      | `ai`          | ai            | High-confidence AI                        |
| 0.20 – 0.75      | `uncertain`   | uncertain     | Signals weak, short text, or disagreement |
| 0.00 – 0.20      | `human`       | human         | High-confidence human                     |

A high-confidence label therefore requires `confidence >= 0.75` on the AI side
and `>= 0.80` on the human side. A confidence of 0.51 and a confidence of 0.95
produce different labels by construction: 0.51 is "Unclear origin"; 0.95 is a
high-confidence label.

The thresholds were 0.20 / 0.80 in the first draft. Calibration showed the LLM
signal is systematically conservative on the AI side (it rarely exceeds 0.85
even on obviously generated text), which compressed the AI end of the scale: only
1 of 15 AI samples crossed 0.80 while the highest-scoring human sample sat at
0.63. Lowering the AI threshold to 0.75 makes the AI label reachable (6 of 15 AI
samples) while keeping a 0.12 margin above every human sample. The asymmetry is
deliberate and provisional; it is re-checked whenever the calibration set grows.

The uncertain band is deliberately wide (55% of the range). The cost of wrongly
telling readers a human's work is AI-made is far higher than the cost of saying
"unclear", so the system only speaks confidently when both signals agree and the
text is long enough to support the statistics.

**Calibration plan (Milestone 4).**

1. Build `calibration/{human,ai,mixed}.json` with labeled samples: 15 human
   (public-domain poems and prose excerpts plus the course's casual blog sample),
   15 AI (generated with Groq by `calibration/generate_ai.py` using varied prompts,
   genres and temperatures, including "sound like a real person" and all-lowercase
   instructions), and 7 mixed (six AI drafts hand-edited by the project author,
   plus the course's "lightly edited AI" sample).
2. Run the pipeline over the set and record `p_llm`, `p_stylo`, `p_ai`.
3. Check: (a) precision of high-confidence calls >= 0.90 on the human and AI
   sets; (b) at least 60% of mixed samples fall in the uncertain band; (c) the
   mean `p_ai` for AI samples is at least 0.35 higher than for human samples;
   (d) no human sample receives `p_ai >=` the AI threshold (0.75).
4. If (d) fails, raise the AI threshold or lower `w_stylo`; if (a) fails on the
   human side, lower the human threshold. Record every change in the Decision log.
5. Publish the resulting score table in the README as the evidence that the
   scores are meaningful.

**Result (2026-10-06, 15 human / 15 AI / 7 mixed, `calibration/run.py --cache`).**

| Set   | mean `p_llm` | mean `p_stylo` | mean `p_ai` | human / uncertain / ai labels |
|-------|--------------|----------------|-------------|-------------------------------|
| human | 0.21         | 0.45           | 0.31        | 5 / 10 / 0                    |
| AI    | 0.66         | 0.76           | 0.69        | 0 / 9 / 6                     |
| mixed | 0.47         | 0.58           | 0.50        | 0 / 7 / 0                     |

All four checks pass: high-confidence precision 11/11, 100% of mixed samples
uncertain, AI-minus-human gap 0.38, maximum human `p_ai` 0.63. Known weak spots:
AI text explicitly prompted to "sound like a real person" (0.39) and all-lowercase
AI social posts (0.48) land in the uncertain band rather than being caught, and
Fitzgerald (0.63) is the human sample closest to the AI line because both signals
read his balanced sentences as polished. The human set is dominated by famous
public-domain prose, which the LLM may partly recognise; adding ordinary modern
human writing is the first thing to do when the set grows.

---

## Transparency label design

Three variants. Each has a short `title` for compact display and a `body` for
the expanded view. The text is plain language for non-technical readers, avoids
the word "detected" as a verdict, and tells the reader what to do with the
information. These exact strings are defined once in `provenance_guard/labels.py`
and copied verbatim into the README.

### Variant `ai` — high-confidence AI (`ai_probability >= 0.75`)

> **Title:** "Likely made with AI"
>
> **Body:** "Our checks found strong signs that this text was produced with an AI writing tool rather than written by a person. The creator has not confirmed this, and may appeal if we got it wrong. Please keep that in mind as you read."

### Variant `human` - high-confidence human (`ai_probability <= 0.20`)

> **Title:** "Likely written by a person"
>
> **Body:** "Our checks found strong signs that a person wrote this text themselves. We found no clear indication of an AI writing tool. No check is perfect, but this piece looks like original human work."

### Variant `uncertain` — uncertain (`0.20 < ai_probability < 0.75`)

> **Title:** "Unclear origin"
>
> **Body:** "We could not tell whether this text was written by a person or produced with an AI writing tool. Our checks disagreed or did not find strong evidence either way. Please read it with that in mind. This is not an accusation."

When content is under appeal, the label body is suffixed with a single sentence:
"The creator has appealed this label and it is under review." The variant does not
change until a reviewer resolves the appeal.

**Review notes.** Label text was revised once before build: the first draft of the
AI variant said "This text was AI-generated", which states a fact the system does
not know. All three variants now use "signs" and "likely", and the uncertain
variant explicitly disclaims accusation.

---

## Appeals workflow

**Who can appeal.** The creator who submitted the content. If the appeal carries a
`creator_id` it must match the submitting creator (403 otherwise) and the appeal
is recorded as `creator_verified: true`. A platform that has already
authenticated the creator may omit `creator_id`; the appeal is then accepted and
recorded as `creator_verified: false` so a reviewer can see the difference. This
matches the course's test call, which sends only `content_id` and
`creator_reasoning`. One open appeal per content item. Appeals
are accepted for any label variant, including `human` (a creator may want an AI
disclosure corrected), but the expected case is `ai` or `uncertain`.

**What they provide.**

| Field          | Required | Rules                                   |
|----------------|----------|-----------------------------------------|
| `content_id`        | yes      | must exist                                                 |
| `creator_reasoning` | yes      | 20–2000 characters, free text (`reasoning` accepted as alias) |
| `creator_id`        | no       | if present, must equal the submitting creator              |
| `evidence_url`      | no       | single URL up to 500 chars, stored as-is, not fetched      |

**What the system does on receipt.**

1. Validate the rules above (400 missing id / 404 unknown / 403 mismatch /
   409 already open / 422 bad reasoning).
2. Create an appeal record: `appeal_id`, `content_id`, `creator_id`, `reasoning`,
   `evidence_url`, `created_at`, `state: "open"`, and a frozen
   `original_decision` snapshot (attribution, `ai_probability`, `confidence`,
   label variant, both signal outputs, the LLM rationale, and the id of the
   original audit entry).
3. Set the content record's `status` from `labeled` to `under_review` and store
   the `appeal_id` on it.
4. Append an audit entry with `type: "appeal"` carrying `appeal_reasoning`,
   `status: "under_review"`, a compact `original_decision` (attribution, scores,
   label variant, both signal scores) and `original_entry_id`, so the decision
   and the appeal sit side by side in the log. Because the log file is
   append-only, the original decision entry is not rewritten; `GET /log` overlays
   `appeal_filed: true`, the `appeal_id` and the current status onto decision
   entries at read time.
5. Return `201` with `received: true`, the appeal id, the new status, the
   original decision summary and the label as it now reads (with the
   under-review sentence appended).

**Status lifecycle.**

```
labeled --(appeal)--> under_review --(reviewer, stretch)--> upheld | overturned
```

Core scope ends at `under_review`. Resolution (`POST /appeals/<id>/resolve` with
`outcome` and `reviewer_note`, which would rewrite the public label) is a stretch
feature and is listed in the Decision log as not-yet-built.

**What a reviewer sees** (`GET /appeals`, newest first):

- content id, creator id, content type, word count, first 300 characters of text
- original label variant, `ai_probability`, `confidence`
- `p_llm` with rationale and indicators; `p_stylo` with the four raw features
- the disagreement value and any shrink applied (so the reviewer sees *why* the
  system was or was not confident)
- the creator's reasoning and evidence URL
- appeal age in hours

The queue is read-only in core scope and is not rate-limited beyond the default
read limit, because it is intended for platform staff behind their own auth.

---

## Anticipated edge cases

1. **Repetitive, simple-vocabulary poetry (villanelles, refrains, children's
   verse).** A villanelle repeats two full lines four times each, which crushes
   `mattr`, and its fixed meter produces near-identical line lengths, which
   drives `sentence_len_cv` toward zero. Stylometry will read this as strongly
   AI-like even when the LLM recognises a classic form. *Mitigation:* the
   disagreement shrink pulls the result into the uncertain band; the `content_type`
   hint lets the LLM prompt note that formal verse is expected to be regular.
   *Residual risk:* if the LLM also leans AI, a human poet gets an AI label and
   must appeal.

2. **Very short pieces (haiku, micro-fiction, a two-line caption).** With 10–30
   words there are not enough sentences for variance statistics. *Mitigation:*
   texts under 15 words are rejected with `422 too_short`; 15–39 words mark
   stylometry unreliable, shift weight to the LLM, apply the 0.60 shrink, and cap
   the result into the uncertain band. Short texts therefore almost always get
   "Unclear origin", which is honest but unsatisfying for a haiku poet.

3. **Hybrid authorship.** A human draft polished by an AI grammar tool, or an AI
   outline expanded by hand. Both signals will land mid-range and the answer
   "unclear" is actually correct, but the label gives the reader no hint that
   partial AI use is the likely story. *Mitigation:* none in core scope; the
   README will note that a fourth "AI-assisted" variant is a possible extension.

4. **Highly formulaic human genres (listicles, product-review blog posts, SEO
   copy).** Human writers in these genres deliberately produce uniform, low-variance
   prose with sparse punctuation, which matches the AI stylometric profile, and
   the LLM also tends to flag "generic" phrasing. *Risk:* false AI labels on
   working bloggers. *Mitigation:* the calibration set includes a few genuine
   human blog paragraphs so the thresholds are not tuned only on literary prose.

5. **Adversarial humanisation.** A user asks a model to "vary sentence length wildly
   and add typos and dashes" to defeat stylometry. `p_stylo` will read human;
   `p_llm` may or may not notice. *Mitigation:* disagreement shrink lands it in
   uncertain rather than human; the system never promises detection of
   deliberately evasive text, and the README says so.

6. **Non-English or heavily dialectal text.** Reference ranges for the four
   features were set for standard English; the clause-marker list is English-only.
   *Mitigation:* out of scope for core; the README states English-only support and
   the LLM prompt is asked to return 0.5 with rationale "non-English" if it cannot
   assess, which forces the uncertain band.

---

## Rate limiting

Implemented with Flask-Limiter, keyed on remote address, in-memory storage
(acceptable for a single-process course project; Redis would be the production
swap).

| Endpoint        | Limit                        | Reasoning                                                                                                   |
|-----------------|------------------------------|-------------------------------------------------------------------------------------------------------------|
| `POST /submit`  | `5 per minute; 100 per day` per IP | Each call makes one Groq request (~1–3 s, ~1.3k prompt tokens plus the text). 5/min covers a person testing or a platform posting as items are published, while stopping a loop from draining the quota. 100/day caps the worst case at roughly 150k tokens per IP per day. |
| `POST /submit`  | `6 per minute` service-wide  | Groq's free tier allows 8,000 tokens per minute for the model; at ~1.3k tokens per call that is about six calls. A shared ceiling turns upstream 429s into a clean local 429 with a retry hint instead of silently degrading to single-signal results. |
| `POST /appeal`  | `5 per hour`                 | A legitimate creator appeals a handful of items at most; this blocks appeal spam without a login system.    |
| `GET /*`        | `60 per minute`              | Reads are cheap; the limit only prevents scraping the log.                                                 |
| `GET /health`   | exempt                       | Liveness probes must never be throttled.                                                                    |

Limit-exceeded responses return `429` with a JSON body
`{"error": "rate_limited", "retry_after_seconds": n}`. The exact strings above are
the ones that appear in `app.py` and are repeated in the README.

---

## Audit log

Append-only JSONL file `audit_log.jsonl`; one JSON object per line. Also mirrored
in memory for `GET /log`. Two entry types.

Decision entry:

```json
{"entry_id": "e_01J9X4K2N1", "type": "decision", "ts": "2026-10-06T14:02:11Z",
 "content_id": "c_01J9X4K2M7", "creator_id": "user_8841", "content_type": "poem",
 "word_count": 212, "attribution": "uncertain", "ai_probability": 0.61, "confidence": 0.61,
 "label_variant": "uncertain",
 "signals": {"llm": {"ai_probability": 0.72, "available": true, "model": "openai/gpt-oss-120b"},
             "stylometry": {"ai_probability": 0.44, "reliable": true,
                            "features": {"sentence_len_cv": 0.52, "mattr": 0.71, "punct_per_100w": 4.1, "clause_markers_per_sent": 1.3}}},
 "combination": {"weights": {"llm": 0.6, "stylometry": 0.4}, "disagreement": 0.28, "shrink_factor": 1.0, "single_signal_clamp": false},
 "status": "labeled", "appeal_filed": false}
```

Appeal entry:

```json
{"entry_id": "e_01J9X5A0PR", "type": "appeal", "ts": "2026-10-06T14:10:40Z",
 "content_id": "c_01J9X4K2M7", "creator_id": "user_8841", "creator_verified": true,
 "appeal_id": "a_01J9X5A0PQ", "original_entry_id": "e_01J9X4K2N1",
 "original_decision": {"attribution": "ai", "ai_probability": 0.86, "confidence": 0.86, "label_variant": "ai",
                       "llm_score": 0.90, "stylometry_score": 0.80},
 "appeal_reasoning": "I wrote this in a notebook over three weeks; I can share dated photos of the drafts.",
 "evidence_url": "https://example.com/drafts",
 "status_before": "labeled", "status": "under_review", "appeal_filed": true}
```

Full text is **not** stored in the log (privacy, log size); the content store
holds it for the lifetime of the process. `GET /log` returns newest first with
`?limit=` (default 50, max 500) and `?type=decision|appeal`. The README will show
at least three real entries captured from a local run.

---

## AI Tool Plan

The implementation milestones use an AI coding assistant. For each one the
plan names what context is handed over, what is requested, and how the output
is checked before it is trusted.

### M3 — submission endpoint + first signal (Groq LLM)

- **Spec sections provided:** Architecture (diagram, narrative, component map),
  API surface (`POST /submit`, `GET /log`), Detection signals -> Signal 1 prompt
  contract and failure handling, Rate limiting table, Audit log decision schema.
- **Ask for:** project layout (`app.py`, `provenance_guard/llm_signal.py`,
  `provenance_guard/labels.py`, `provenance_guard/store.py`,
  `provenance_guard/audit.py`), the Flask app with `/submit`, `/health`,
  `/content/<id>` and `/log` wired to Flask-Limiter using the exact limit strings,
  and a function `llm_signal.score(text, content_type) -> LlmResult` that calls
  Groq in JSON mode, validates the reply, and returns `available: false` on any
  failure. Until the combiner exists, `/submit` derives attribution from the LLM
  probability alone and clamps an unavailable signal into [0.25, 0.70].
- **Verify:** run `scripts/probe_llm_signal.py`, which calls `score` directly on
  (a) a public-domain Melville paragraph, (b) a deliberately generic blog
  paragraph, (c) the 29-word course sample, (d) a prompt-injection attempt.
  Expect (a) well below 0.5, (b) well above, (c) near 0.5, (d) not driven to 0.0
  by the injected instruction, and every result with `available: true`. Then post
  the same texts to `/submit` with `curl`, confirm the response carries
  `content_id`, `attribution`, `confidence` and `label`, confirm `GET /log` shows
  one structured entry per call, and confirm the eleventh request in a minute
  returns `429` with `retry_after_seconds` (the limit was 10/min at the time; see Rate limiting).

### M4 — second signal (stylometry) + confidence scoring

- **Spec sections provided:** Detection signals (Signal 2 feature table and
  formula, and the Combination pseudocode), Uncertainty representation (bands +
  calibration plan), Architecture diagram.
- **Ask for:** a pure function `stylometry.score(text) -> StylometryResult`
  returning the four features, four sub-scores, `p_stylo`, and `reliable`;
  `provenance_guard/combine.py` implementing the pseudocode exactly; replacing
  the placeholder scoring in `/submit` with the combiner; and `calibration/run.py`
  that scores every sample in `calibration/{human,ai,mixed}.json` and prints a
  table of `p_llm, p_stylo, p_ai, band` plus the four acceptance checks.
- **Check:** run `stylometry.score` by hand on the same three texts as M3 and
  confirm Melville < 0.5 with `reliable: true`, the generic blog > 0.5, and the
  haiku-length text `reliable: false`, with every sub-score inside [0, 1]. Then
  run `calibration/run.py` and apply the four acceptance checks from the
  calibration plan: no human sample reaches the AI threshold, the AI/human mean gap is
  >= 0.35, mixed samples mostly land in the uncertain band, and setting a bad API
  key yields `available: false` with a result clamped inside [0.25, 0.70]. Record
  threshold or weight changes in the Decision log.

### M5 - production layer (labels + appeals + log)

- **Spec sections provided:** Transparency label design (all three variants,
  verbatim), Appeals workflow, Audit log schemas, Architecture diagram, API
  surface for `/appeal`, `/appeals`, `/content/<id>`, `/log`.
- **Ask for:** `provenance_guard/labels.py` holding the three label constants and
  `label_for(p_ai, under_review: bool)`; the `/appeal` endpoint with the five
  receipt steps and the four error cases; `/appeals` reviewer view; `/log` with
  `limit` and `type` filters; and a `tests/` folder with pytest cases.
- **Verify:** tests must show all three variants are reachable by driving
  `label_for` with 0.10, 0.50, 0.90, and by posting three calibration texts that
  land in each band through `/submit`. Appeal test: submit, appeal, then
  `GET /content/<id>` shows `under_review`, `GET /appeals` lists it with the
  original decision snapshot, `GET /log?type=appeal` shows the entry referencing
  the original `entry_id`, and a second appeal returns `409`. Finally, copy the
  label strings from `labels.py` into the README and diff them against this
  document to confirm they are identical.

### Working rules for all milestones

- The assistant generates against the sections above; anything not in the spec
  is decided here first and the spec is updated before code changes.
- Every generated function is run by hand with at least one input before it is
  wired into a route.
- Stretch features (appeal resolution, SQLite persistence, an "AI-assisted"
  fourth label) require a planning.md update before any code.

---

## Decision log

| Date       | Decision                                                            | Reason                                                                  |
|------------|---------------------------------------------------------------------|-------------------------------------------------------------------------|
| 2026-10-05 | Two signals: Groq LLM (semantic) + stylometry (structural)          | Required to be distinct; these fail differently                         |
| 2026-10-05 | Weights 0.6 / 0.4, bands at 0.20 / 0.80                             | Favor "unclear" over false AI accusations; to be tuned in M4            |
| 2026-10-05 | Single-signal results clamped inside the uncertain band             | Brief forbids single-signal classification                              |
| 2026-10-05 | Label text revised from "was AI-generated" to "likely made with AI" | System estimates, it does not know                                      |
| 2026-10-05 | Appeal resolution endpoint deferred to stretch                      | Brief requires only `under_review`; keep core scope small               |
| 2026-10-05 | In-memory store + JSONL log, no database                            | Single-process course project; swap to SQLite is isolated in `store.py` |
| 2026-10-06 | M3 builds the Groq signal first; stylometry moves to M4                | Milestone guidance and its sample log entry assume the LLM is signal 1 |
| 2026-10-06 | Default model `openai/gpt-oss-120b`, overridable via `GROQ_MODEL`      | `llama-3.3-70b-versatile` was retired from Groq before M3 began        |
| 2026-10-06 | `mattr` direction flipped (high diversity = AI-like); ranges retuned; weights 0.35/0.30/0.20/0.15 | Calibration AUC for `mattr` was 0.16 under the original assumption; stylometry AUC rose 0.69 -> 0.91 |
| 2026-10-06 | LLM prompt rewritten with explicit anti-mistake rules and concrete tells | Original prompt scored AUC 0.54; new prompt 0.95 on the same 37 texts   |
| 2026-10-06 | AI threshold lowered 0.80 -> 0.75; human threshold unchanged at 0.20    | LLM is conservative on the AI side; max human p_ai 0.63 leaves margin   |
| 2026-10-06 | `/submit` limits: 5/min + 100/day per IP, plus 6/min service-wide       | Groq free tier is 8k tokens/min for the model (~6 calls/min)             |
| 2026-10-06 | Keep `gpt-oss-120b` over `qwen3.8-27b` despite Qwen's AUC 1.00            | Qwen's human scores are memorised-classic recognition and near-binary; it calls edited AI drafts human |
| 2026-10-06 | `creator_id` optional on `/appeal`; recorded as `creator_verified`       | Course test call sends only `content_id` + `creator_reasoning`           |
| 2026-10-06 | Single-signal clamp derived from thresholds (0.25 .. 0.70)              | Test caught a clamped result landing exactly on the 0.75 AI line         |
| 2026-10-06 | `GET /log` overlays appeal state onto decision entries at read time      | File stays append-only yet the log shows whether an appeal was filed     |
