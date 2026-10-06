"""Provenance Guard - Flask entry point.

Routes
  POST /submit          classify text with both signals, label it, log it
  POST /appeal          creator contests a decision -> status "under_review", logged
  GET  /content/<id>    current status, decision and label for one item
  GET  /appeals         reviewer queue of open appeals with original decisions
  GET  /log             structured audit log, newest first
  GET  /health          liveness
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from provenance_guard import llm_signal, stylometry
from provenance_guard.audit import AuditLog
from provenance_guard.combine import combine
from provenance_guard.labels import label_for
from provenance_guard.store import ContentStore

load_dotenv()

# --- Rate limits (documented in planning.md and README; keep in sync) --------
SUBMIT_LIMIT = "5 per minute;100 per day"   # per client IP
SUBMIT_GLOBAL_LIMIT = "6 per minute"        # whole service; matches Groq free-tier 8k tokens/min
APPEAL_LIMIT = "5 per hour"                 # per client IP
READ_LIMIT = "60 per minute"                # per client IP, all GET routes except /health

# --- Input bounds -------------------------------------------------------------
MIN_WORDS = 15
MAX_WORDS = 20_000
CONTENT_TYPES = {"poem", "story", "blog", "other"}
MIN_REASONING_CHARS = 20
MAX_REASONING_CHARS = 2000
EXCERPT_CHARS = 300


def _error(status: int, code: str, message: str, **extra):
    body = {"error": code, "message": message, **extra}
    return jsonify(body), status


def _age_hours(iso_ts: str) -> float:
    created = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return round((datetime.now(timezone.utc) - created).total_seconds() / 3600, 2)


def create_app(store: ContentStore | None = None, audit: AuditLog | None = None) -> Flask:
    app = Flask(__name__)
    app.json.sort_keys = False
    store = store if store is not None else ContentStore()
    audit = audit if audit is not None else AuditLog()  # an empty log is falsy, so no "or"

    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[READ_LIMIT],
        storage_uri="memory://",
        headers_enabled=True,
    )

    @app.errorhandler(429)
    def rate_limited(exc):
        retry_after = None
        try:
            current = limiter.current_limit
            if current is not None and current.reset_at:
                retry_after = max(0, int(current.reset_at - time.time()))
        except Exception:
            retry_after = None
        return _error(
            429,
            "rate_limited",
            f"Rate limit exceeded: {getattr(exc, 'description', '')}".strip(),
            retry_after_seconds=retry_after,
        )

    @app.errorhandler(404)
    def not_found(_exc):
        return _error(404, "not_found", "No such route or resource.")

    # ------------------------------------------------------------------------
    @app.get("/health")
    @limiter.exempt
    def health():
        return jsonify({"status": "ok", "entries_logged": len(audit)})

    # ------------------------------------------------------------------------
    @app.post("/submit")
    @limiter.limit(SUBMIT_LIMIT)
    @limiter.limit(SUBMIT_GLOBAL_LIMIT, key_func=lambda: "global", scope="submit_global")
    def submit():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error(400, "invalid_json", "Body must be a JSON object.")

        text = payload.get("text")
        creator_id = payload.get("creator_id")
        content_type = payload.get("content_type", "other")

        if not isinstance(text, str) or not text.strip():
            return _error(400, "missing_field", "'text' is required and must be a non-empty string.")
        if not isinstance(creator_id, str) or not creator_id.strip():
            return _error(400, "missing_field", "'creator_id' is required and must be a non-empty string.")
        if content_type not in CONTENT_TYPES:
            return _error(400, "invalid_field", f"'content_type' must be one of {sorted(CONTENT_TYPES)}.")

        text = text.strip()
        word_count = len(text.split())
        if word_count < MIN_WORDS:
            return _error(422, "too_short", f"Text must be at least {MIN_WORDS} words.", word_count=word_count)
        if word_count > MAX_WORDS:
            return _error(413, "too_long", f"Text must be at most {MAX_WORDS} words.", word_count=word_count)

        # --- Detection pipeline ---------------------------------------------
        stylo = stylometry.score(text)                 # Signal 2, local
        llm = llm_signal.score(text, content_type)     # Signal 1, Groq
        combined = combine(llm, stylo, word_count)
        p_ai, confidence = combined.ai_probability, combined.confidence
        label = label_for(p_ai)

        decision = {
            "attribution": label["variant"],
            "ai_probability": p_ai,
            "confidence": confidence,
            "label": label,
            "signals": {
                "llm": llm.to_dict(),
                "stylometry": stylo.to_dict(),
            },
            "combination": combined.to_dict(),
        }

        record = store.create_content(
            creator_id=creator_id.strip(),
            text=text,
            content_type=content_type,
            word_count=word_count,
            decision=decision,
        )

        entry = audit.append(
            "decision",
            {
                "content_id": record["content_id"],
                "creator_id": record["creator_id"],
                "content_type": content_type,
                "word_count": word_count,
                "attribution": decision["attribution"],
                "ai_probability": p_ai,
                "confidence": confidence,
                "label_variant": label["variant"],
                "signals": {
                    "llm": {
                        "ai_probability": llm.ai_probability,
                        "available": llm.available,
                        "model": llm.model,
                        "error": llm.error,
                    },
                    "stylometry": {
                        "ai_probability": stylo.ai_probability,
                        "reliable": stylo.reliable,
                        "features": stylo.features,
                    },
                },
                "combination": {
                    "weights": combined.weights,
                    "disagreement": combined.disagreement,
                    "shrink_factor": combined.shrink_factor,
                    "single_signal_clamp": combined.single_signal_clamp,
                },
                "status": record["status"],
                "appeal_filed": False,
            },
        )
        store.set_audit_entry_id(record["content_id"], entry["entry_id"])

        return (
            jsonify(
                {
                    "content_id": record["content_id"],
                    "status": record["status"],
                    "attribution": decision["attribution"],
                    "ai_probability": p_ai,
                    "confidence": confidence,
                    "label": label,
                    "signals": decision["signals"],
                    "combination": decision["combination"],
                    "created_at": record["created_at"],
                }
            ),
            201,
        )

    # ------------------------------------------------------------------------
    @app.post("/appeal")
    @limiter.limit(APPEAL_LIMIT)
    def appeal():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error(400, "invalid_json", "Body must be a JSON object.")

        content_id = payload.get("content_id")
        creator_id = payload.get("creator_id")
        # Course API name is `creator_reasoning`; `reasoning` is accepted as an alias.
        reasoning = payload.get("creator_reasoning", payload.get("reasoning"))
        evidence_url = payload.get("evidence_url")

        if not isinstance(content_id, str) or not content_id.strip():
            return _error(400, "missing_field", "'content_id' is required.")
        if not isinstance(reasoning, str) or not (MIN_REASONING_CHARS <= len(reasoning.strip()) <= MAX_REASONING_CHARS):
            return _error(
                422, "invalid_reasoning",
                f"'creator_reasoning' must be {MIN_REASONING_CHARS}-{MAX_REASONING_CHARS} characters.",
            )
        if evidence_url is not None and (not isinstance(evidence_url, str) or len(evidence_url) > 500):
            return _error(422, "invalid_field", "'evidence_url' must be a string of at most 500 characters.")

        record = store.get_content(content_id.strip())
        if record is None:
            return _error(404, "not_found", "Unknown content_id.")

        # Ownership: enforced when a creator_id is supplied; a platform that has
        # already authenticated the creator may omit it (recorded as unverified).
        creator_verified = False
        if creator_id is not None:
            if not isinstance(creator_id, str) or creator_id.strip() != record["creator_id"]:
                return _error(403, "creator_mismatch", "creator_id does not match the submitting creator.")
            creator_verified = True

        if record["status"] == "under_review":
            return _error(409, "appeal_open", "An appeal is already open for this content.", appeal_id=record["appeal_id"])

        decision = record["decision"]
        original_decision = {
            "attribution": decision["attribution"],
            "ai_probability": decision["ai_probability"],
            "confidence": decision["confidence"],
            "label_variant": decision["label"]["variant"],
            "signals": {
                "llm": {
                    "ai_probability": decision["signals"]["llm"]["ai_probability"],
                    "available": decision["signals"]["llm"]["available"],
                    "rationale": decision["signals"]["llm"].get("rationale"),
                    "indicators": decision["signals"]["llm"].get("indicators", []),
                },
                "stylometry": {
                    "ai_probability": decision["signals"]["stylometry"]["ai_probability"],
                    "reliable": decision["signals"]["stylometry"]["reliable"],
                    "features": decision["signals"]["stylometry"]["features"],
                },
            },
            "combination": decision["combination"],
            "decided_at": record["created_at"],
        }

        status_before = record["status"]
        appeal_record = store.create_appeal(
            content_id=record["content_id"],
            creator_id=record["creator_id"],
            reasoning=reasoning.strip(),
            evidence_url=evidence_url,
            original_decision=original_decision,
            original_entry_id=record["audit_entry_id"],
        )
        appeal_record["creator_verified"] = creator_verified

        audit.append(
            "appeal",
            {
                "content_id": record["content_id"],
                "creator_id": record["creator_id"],
                "creator_verified": creator_verified,
                "appeal_id": appeal_record["appeal_id"],
                "original_entry_id": record["audit_entry_id"],
                "original_decision": {
                    "attribution": original_decision["attribution"],
                    "ai_probability": original_decision["ai_probability"],
                    "confidence": original_decision["confidence"],
                    "label_variant": original_decision["label_variant"],
                    "llm_score": original_decision["signals"]["llm"]["ai_probability"],
                    "stylometry_score": original_decision["signals"]["stylometry"]["ai_probability"],
                },
                "appeal_reasoning": appeal_record["reasoning"],
                "evidence_url": evidence_url,
                "status_before": status_before,
                "status": "under_review",
                "appeal_filed": True,
            },
        )

        return (
            jsonify(
                {
                    "received": True,
                    "appeal_id": appeal_record["appeal_id"],
                    "content_id": record["content_id"],
                    "status": "under_review",
                    "creator_verified": creator_verified,
                    "original_decision": {
                        "attribution": original_decision["attribution"],
                        "ai_probability": original_decision["ai_probability"],
                        "confidence": original_decision["confidence"],
                        "label_variant": original_decision["label_variant"],
                    },
                    "label": label_for(decision["ai_probability"], under_review=True),
                    "created_at": appeal_record["created_at"],
                }
            ),
            201,
        )

    # ------------------------------------------------------------------------
    @app.get("/content/<content_id>")
    def get_content(content_id: str):
        record = store.get_content(content_id)
        if record is None:
            return _error(404, "not_found", "Unknown content_id.")
        under_review = record["status"] == "under_review"
        return jsonify(
            {
                "content_id": record["content_id"],
                "creator_id": record["creator_id"],
                "content_type": record["content_type"],
                "word_count": record["word_count"],
                "status": record["status"],
                "appeal_id": record["appeal_id"],
                "attribution": record["decision"]["attribution"],
                "ai_probability": record["decision"]["ai_probability"],
                "confidence": record["decision"]["confidence"],
                "label": label_for(record["decision"]["ai_probability"], under_review),
                "created_at": record["created_at"],
            }
        )

    # ------------------------------------------------------------------------
    @app.get("/appeals")
    def list_appeals():
        """Reviewer queue. Read-only; intended to sit behind platform staff auth."""
        queue = []
        for a in store.open_appeals():
            content = store.get_content(a["content_id"])
            queue.append(
                {
                    "appeal_id": a["appeal_id"],
                    "content_id": a["content_id"],
                    "creator_id": a["creator_id"],
                    "creator_verified": a.get("creator_verified", False),
                    "content_type": content["content_type"] if content else None,
                    "word_count": content["word_count"] if content else None,
                    "excerpt": (content["text"][:EXCERPT_CHARS] if content else None),
                    "original_decision": a["original_decision"],
                    "creator_reasoning": a["reasoning"],
                    "evidence_url": a["evidence_url"],
                    "submitted_at": a["created_at"],
                    "age_hours": _age_hours(a["created_at"]),
                    "state": a["state"],
                }
            )
        return jsonify({"count": len(queue), "appeals": queue})

    # ------------------------------------------------------------------------
    @app.get("/log")
    def get_log():
        try:
            limit = int(request.args.get("limit", 50))
        except ValueError:
            return _error(400, "invalid_field", "'limit' must be an integer.")
        entry_type = request.args.get("type")
        if entry_type not in (None, "decision", "appeal"):
            return _error(400, "invalid_field", "'type' must be 'decision' or 'appeal'.")
        entries = audit.entries(limit=limit, entry_type=entry_type)

        # The file is append-only, so a decision entry cannot be edited when an
        # appeal arrives later. Overlay the current appeal state at read time.
        enriched = []
        for e in entries:
            if e.get("type") == "decision":
                content = store.get_content(e.get("content_id", ""))
                if content and content.get("appeal_id"):
                    e = {**e, "appeal_filed": True, "appeal_id": content["appeal_id"], "status": content["status"]}
            enriched.append(e)
        return jsonify({"count": len(enriched), "total": len(audit), "entries": enriched})

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
