"""Score the calibration set and apply the acceptance checks from planning.md.

    python calibration/run.py            # both signals (needs GROQ_API_KEY)
    python calibration/run.py --no-llm   # stylometry only, offline
    python calibration/run.py --cache    # reuse LLM scores saved in llm_cache.json

Checks (planning.md, "Calibration plan"):
  (a) precision of high-confidence calls >= 0.90 on human and AI sets
  (b) >= 60% of mixed samples land in the uncertain band
  (c) mean p_ai(AI) - mean p_ai(human) >= 0.35
  (d) no human sample receives p_ai >= AI_THRESHOLD
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from provenance_guard import llm_signal, stylometry  # noqa: E402
from provenance_guard.combine import combine  # noqa: E402
from provenance_guard.labels import AI_THRESHOLD, band  # noqa: E402
from provenance_guard.llm_signal import LlmResult  # noqa: E402

HERE = Path(__file__).parent
CACHE = HERE / "llm_cache.json"
SETS = ["human", "ai", "mixed"]


def load(name: str) -> list[dict]:
    p = HERE / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def main(argv: list[str]) -> int:
    use_llm = "--no-llm" not in argv
    use_cache = "--cache" in argv
    cache: dict = json.loads(CACHE.read_text(encoding="utf-8")) if (use_cache and CACHE.exists()) else {}

    rows: list[dict] = []
    for set_name in SETS:
        for s in load(set_name):
            st = stylometry.score(s["text"])
            if use_llm:
                if s["id"] in cache:
                    ll = LlmResult(**cache[s["id"]])
                else:
                    ll = llm_signal.score(s["text"], s["content_type"])
                    cache[s["id"]] = ll.to_dict()
            else:
                ll = LlmResult(ai_probability=0.5, rationale="skipped", available=False)
            c = combine(ll, st, st.word_count)
            rows.append(
                {
                    "set": set_name, "id": s["id"], "words": st.word_count, "reliable": st.reliable,
                    "p_llm": ll.ai_probability if ll.available else None, "llm_ok": ll.available,
                    "p_stylo": st.ai_probability, **{k: v for k, v in st.features.items()},
                    "p_ai": c.ai_probability, "band": band(c.ai_probability),
                    "d": c.disagreement, "shrink": c.shrink_factor,
                }
            )
    if use_llm:
        CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    # --- table ---------------------------------------------------------------
    print(f"{'set':6} {'id':26} {'w':>4} {'rel':>3} {'p_llm':>5} {'p_sty':>5} | {'cv':>4} {'mattr':>5} {'punct':>5} {'claus':>5} | {'p_ai':>5} {'d':>4} {'shr':>4} band")
    print("-" * 118)
    for r in rows:
        p_llm = f"{r['p_llm']:>5.2f}" if r["p_llm"] is not None else "  n/a"
        print(
            f"{r['set']:6} {r['id']:26} {r['words']:>4} {('y' if r['reliable'] else 'n'):>3} {p_llm} {r['p_stylo']:>5.2f} | "
            f"{r['sentence_len_cv']:>4.2f} {r['mattr']:>5.2f} {r['punct_per_100w']:>5.1f} {r['clause_markers_per_sent']:>5.2f} | "
            f"{r['p_ai']:>5.2f} {r['d']:>4.2f} {r['shrink']:>4.2f} {r['band']}"
        )

    # --- summary + checks ----------------------------------------------------
    def rows_of(name): return [r for r in rows if r["set"] == name]
    def mean(xs): return statistics.mean(xs) if xs else float("nan")

    print("\nPer-set means")
    for name in SETS:
        rs = rows_of(name)
        if not rs:
            continue
        pl = [r["p_llm"] for r in rs if r["p_llm"] is not None]
        print(f"  {name:6} n={len(rs):>2}  p_llm={mean(pl):.2f}  p_stylo={mean([r['p_stylo'] for r in rs]):.2f}  p_ai={mean([r['p_ai'] for r in rs]):.2f}"
              f"  bands: " + ", ".join(f"{b}={sum(1 for r in rs if r['band']==b)}" for b in ("human", "uncertain", "ai")))

    human, ai, mixed = rows_of("human"), rows_of("ai"), rows_of("mixed")
    confident = [(r, "human") for r in human if r["band"] != "uncertain"] + [(r, "ai") for r in ai if r["band"] != "uncertain"]
    correct = sum(1 for r, truth in confident if r["band"] == truth)
    prec = correct / len(confident) if confident else float("nan")
    gap = mean([r["p_ai"] for r in ai]) - mean([r["p_ai"] for r in human]) if ai and human else float("nan")
    mixed_unc = sum(1 for r in mixed if r["band"] == "uncertain") / len(mixed) if mixed else float("nan")
    worst_human = max((r["p_ai"] for r in human), default=float("nan"))

    checks = [
        ("(a) high-confidence precision >= 0.90", prec >= 0.90, f"{correct}/{len(confident)} = {prec:.2f}"),
        ("(b) mixed in uncertain band >= 60%", mixed_unc >= 0.60, f"{mixed_unc:.0%}"),
        ("(c) mean gap AI - human >= 0.35", gap >= 0.35, f"{gap:.2f}"),
        (f"(d) no human sample p_ai >= {AI_THRESHOLD:.2f}", worst_human < AI_THRESHOLD, f"max human p_ai = {worst_human:.2f}"),
    ]
    print("\nAcceptance checks")
    ok = True
    for name, passed, detail in checks:
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name:40} {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
