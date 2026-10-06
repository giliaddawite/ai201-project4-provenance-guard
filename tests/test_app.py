"""End-to-end tests for the Flask app. The Groq signal is stubbed so the suite is
deterministic and runs offline.

    python -m pytest -q
"""

from __future__ import annotations

import json

import pytest

import app as app_module
from provenance_guard import labels, llm_signal
from provenance_guard.audit import AuditLog
from provenance_guard.llm_signal import LlmResult
from provenance_guard.store import ContentStore

# Three texts chosen so that, with the stubbed LLM scores below, the combined
# score lands in each band. Each is long enough (>= 80 words) to avoid the
# short-text shrink.
HUMAN_TEXT = (
    "True!—nervous—very, very dreadfully nervous I had been and am; but why will you say that "
    "I am mad? The disease had sharpened my senses—not destroyed—not dulled them. Above all was "
    "the sense of hearing acute. I heard all things in the heaven and in the earth. I heard many "
    "things in hell. How, then, am I mad? Hearken! and observe how healthily—how calmly I can tell "
    "you the whole story. It is impossible to say how first the idea entered my brain; but once "
    "conceived, it haunted me day and night. Object there was none. Passion there was none. I "
    "loved the old man. He had never wronged me. He had never given me insult."
)
AI_TEXT = (
    "Artificial intelligence represents a transformative paradigm shift in modern society. It is "
    "important to note that while the benefits of AI are numerous, it is equally essential to "
    "consider the ethical implications. Furthermore, stakeholders across various sectors must "
    "collaborate to ensure responsible deployment. Organizations should establish clear "
    "governance frameworks that balance innovation with accountability. Educational institutions "
    "must prepare students for an evolving landscape. Policymakers need to craft regulations that "
    "protect citizens without stifling progress. Ultimately, the responsible development of "
    "artificial intelligence requires sustained collaboration between technologists, ethicists, "
    "and communities. By working together, society can harness these powerful tools for the "
    "benefit of all."
)
MIXED_TEXT = (
    "I've been thinking a lot about remote work lately. There are genuine tradeoffs, flexibility "
    "and no commute on one side, isolation and blurred work-life boundaries on the other. Studies "
    "show productivity varies widely by individual and role type. My own experience has been "
    "mixed. Some weeks I get more done than I ever did in the office; other weeks the days blur "
    "together and I realize at four that I have not spoken to anyone. The honest answer is that it "
    "depends on the person, the manager, and whether the kitchen table counts as a desk."
)

STUBBED_LLM = {
    HUMAN_TEXT: 0.05,
    AI_TEXT: 0.90,
    MIXED_TEXT: 0.50,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    def fake_score(text, content_type="other", **_kwargs):
        if text in STUBBED_LLM:
            return LlmResult(ai_probability=STUBBED_LLM[text], rationale="stub", indicators=["stub"])
        return LlmResult(ai_probability=0.5, rationale="stub-default", indicators=[])

    monkeypatch.setattr(llm_signal, "score", fake_score)
    monkeypatch.setattr(app_module.llm_signal, "score", fake_score)

    store = ContentStore()
    audit = AuditLog(tmp_path / "audit.jsonl")
    flask_app = app_module.create_app(store=store, audit=audit)
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        c.audit_path = tmp_path / "audit.jsonl"
        yield c


def submit(client, text, creator="tester", content_type="blog"):
    return client.post("/submit", json={"text": text, "creator_id": creator, "content_type": content_type})


# --- labels -----------------------------------------------------------------

def test_label_for_reaches_all_three_variants():
    assert labels.label_for(0.10)["variant"] == "human"
    assert labels.label_for(0.50)["variant"] == "uncertain"
    assert labels.label_for(0.90)["variant"] == "ai"
    # boundaries
    assert labels.label_for(labels.HUMAN_THRESHOLD)["variant"] == "human"
    assert labels.label_for(labels.AI_THRESHOLD)["variant"] == "ai"
    assert labels.label_for(labels.HUMAN_THRESHOLD + 0.001)["variant"] == "uncertain"


def test_label_text_matches_spec():
    assert labels.LABELS["ai"]["title"] == "Likely made with AI"
    assert labels.LABELS["human"]["title"] == "Likely written by a person"
    assert labels.LABELS["uncertain"]["title"] == "Unclear origin"
    assert labels.LABELS["uncertain"]["body"].endswith("This is not an accusation.")
    assert labels.label_for(0.9, under_review=True)["body"].endswith(labels.UNDER_REVIEW_SUFFIX)


def test_submit_reaches_all_three_bands(client):
    seen = {}
    for text in (HUMAN_TEXT, AI_TEXT, MIXED_TEXT):
        r = submit(client, text)
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        seen[d["label"]["variant"]] = d
    assert set(seen) == {"human", "uncertain", "ai"}
    # the label text differs per band
    assert len({d["label"]["body"] for d in seen.values()}) == 3
    # 0.51-style vs 0.95-style confidence produce different labels
    assert seen["ai"]["confidence"] > 0.75 and seen["uncertain"]["confidence"] < 0.75


# --- submit contract ----------------------------------------------------------

def test_submit_response_shape_and_log(client):
    r = submit(client, AI_TEXT, creator="u1")
    d = r.get_json()
    for key in ("content_id", "status", "attribution", "ai_probability", "confidence", "label", "signals", "combination"):
        assert key in d
    assert d["status"] == "labeled"
    assert d["signals"]["llm"]["ai_probability"] == 0.90
    assert 0.0 <= d["signals"]["stylometry"]["ai_probability"] <= 1.0

    log = client.get("/log").get_json()
    assert log["total"] == 1
    e = log["entries"][0]
    assert e["type"] == "decision" and e["content_id"] == d["content_id"]
    assert e["signals"]["llm"]["ai_probability"] == 0.90
    assert "features" in e["signals"]["stylometry"]
    assert e["appeal_filed"] is False
    # on disk, one JSON object per line
    lines = client.audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["content_id"] == d["content_id"]


def test_submit_validation(client):
    assert client.post("/submit", data="nope", content_type="application/json").status_code == 400
    assert client.post("/submit", json={"text": AI_TEXT}).status_code == 400
    assert client.post("/submit", json={"text": "too short", "creator_id": "x"}).status_code == 422
    assert client.post("/submit", json={"text": AI_TEXT, "creator_id": "x", "content_type": "song"}).status_code == 400


def test_single_signal_is_clamped_when_llm_unavailable(client, monkeypatch):
    def down(text, content_type="other", **_):
        return LlmResult(ai_probability=0.5, rationale="down", available=False, error="api_error: test")

    monkeypatch.setattr(app_module.llm_signal, "score", down)
    d = submit(client, AI_TEXT).get_json()
    assert d["signals"]["llm"]["available"] is False
    assert 0.25 <= d["ai_probability"] <= 0.75
    assert d["label"]["variant"] == "uncertain"
    assert d["combination"]["single_signal_clamp"] is True


# --- appeals -------------------------------------------------------------------

def test_appeal_flow(client):
    cid = submit(client, AI_TEXT, creator="poet-1").get_json()["content_id"]

    r = client.post("/appeal", json={
        "content_id": cid,
        "creator_id": "poet-1",
        "creator_reasoning": "I wrote this myself from personal experience and can share my drafts.",
    })
    assert r.status_code == 201, r.get_json()
    a = r.get_json()
    assert a["received"] is True and a["status"] == "under_review" and a["creator_verified"] is True
    assert a["original_decision"]["label_variant"] == "ai"
    assert a["label"]["body"].endswith(labels.UNDER_REVIEW_SUFFIX)

    # content status flipped, label carries the suffix
    c = client.get(f"/content/{cid}").get_json()
    assert c["status"] == "under_review" and c["appeal_id"] == a["appeal_id"]
    assert c["label"]["body"].endswith(labels.UNDER_REVIEW_SUFFIX)

    # reviewer queue shows it with the original decision and both signal scores
    q = client.get("/appeals").get_json()
    assert q["count"] == 1
    item = q["appeals"][0]
    assert item["content_id"] == cid and item["creator_reasoning"].startswith("I wrote this myself")
    assert item["original_decision"]["signals"]["llm"]["ai_probability"] == 0.90
    assert "features" in item["original_decision"]["signals"]["stylometry"]

    # audit log: appeal entry references the original decision entry
    log = client.get("/log").get_json()
    assert log["total"] == 2
    appeal_entry, decision_entry = log["entries"]
    assert appeal_entry["type"] == "appeal"
    assert appeal_entry["status"] == "under_review"
    assert appeal_entry["appeal_reasoning"].startswith("I wrote this myself")
    assert appeal_entry["original_entry_id"] == decision_entry["entry_id"]
    assert appeal_entry["original_decision"]["label_variant"] == "ai"
    # decision entry is overlaid with current appeal state at read time
    assert decision_entry["appeal_filed"] is True and decision_entry["status"] == "under_review"
    only_appeals = client.get("/log?type=appeal").get_json()
    assert only_appeals["count"] == 1

    # second appeal is rejected
    r2 = client.post("/appeal", json={"content_id": cid, "creator_reasoning": "Please look again at this piece."})
    assert r2.status_code == 409


def test_appeal_without_creator_id_is_accepted_but_unverified(client):
    cid = submit(client, MIXED_TEXT, creator="u9").get_json()["content_id"]
    r = client.post("/appeal", json={"content_id": cid, "creator_reasoning": "This is my own writing; I am a non-native speaker."})
    assert r.status_code == 201
    assert r.get_json()["creator_verified"] is False


def test_appeal_errors(client):
    cid = submit(client, HUMAN_TEXT, creator="owner").get_json()["content_id"]
    assert client.post("/appeal", json={"content_id": "c_missing", "creator_reasoning": "x" * 30}).status_code == 404
    assert client.post("/appeal", json={"content_id": cid, "creator_id": "someone-else", "creator_reasoning": "x" * 30}).status_code == 403
    assert client.post("/appeal", json={"content_id": cid, "creator_reasoning": "short"}).status_code == 422
    assert client.post("/appeal", json={"creator_reasoning": "x" * 30}).status_code == 400


# --- rate limiting ------------------------------------------------------------

def test_submit_rate_limit_returns_429_with_retry_hint(client):
    codes = []
    for _ in range(7):
        codes.append(client.post("/submit", json={"text": "short", "creator_id": "flood"}).status_code)
    # per-IP limit is 5/minute; validation failures still count
    assert codes[:5] == [422] * 5
    assert codes[5] == 429 and codes[6] == 429
    body = client.post("/submit", json={"text": "short", "creator_id": "flood"}).get_json()
    assert body["error"] == "rate_limited" and "retry_after_seconds" in body


def test_health_is_not_rate_limited(client):
    for _ in range(70):
        assert client.get("/health").status_code == 200
