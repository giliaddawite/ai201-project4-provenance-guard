"""Compare LLM-signal variants (prompt, model, reasoning effort) on the calibration set.

Prints, per variant: mean p_llm for human / AI / mixed, the AUC of p_llm as an
AI detector (human vs AI sets), and the worst-scored human and AI samples.
Results are cached per (variant, sample id) in prompt_experiments_cache.json.

    python calibration/prompt_experiments.py            # all variants
    python calibration/prompt_experiments.py v1 v3      # selected variants
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

from provenance_guard import llm_signal  # noqa: E402

HERE = Path(__file__).parent
CACHE = HERE / "prompt_experiments_cache.json"

IMPROVED_PROMPT = llm_signal.SYSTEM_PROMPT  # adopted as the production prompt on 2026-10-06

VARIANTS: dict[str, dict] = {
    "v0_baseline_gptoss120b": {"model": "openai/gpt-oss-120b", "system_prompt": None, "reasoning_effort": None},
    "v1_improved_gptoss120b": {"model": "openai/gpt-oss-120b", "system_prompt": IMPROVED_PROMPT, "reasoning_effort": None},
    "v2_improved_gptoss120b_high": {"model": "openai/gpt-oss-120b", "system_prompt": IMPROVED_PROMPT, "reasoning_effort": "high"},
    "v3_improved_qwen27b": {"model": "qwen/qwen3.8-27b", "system_prompt": IMPROVED_PROMPT, "reasoning_effort": None},
    "v4_improved_gptoss20b": {"model": "openai/gpt-oss-20b", "system_prompt": IMPROVED_PROMPT, "reasoning_effort": None},
}


def auc(pos: list[float], neg: list[float]) -> float:
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg)) if pos and neg else float("nan")


def load(name: str) -> list[dict]:
    return json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"))


def main(argv: list[str]) -> None:
    wanted = [v for v in VARIANTS if not argv or any(v.startswith(a) for a in argv)]
    cache: dict = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    sets = {name: load(name) for name in ("human", "ai", "mixed")}

    for vname in wanted:
        cfg = VARIANTS[vname]
        vcache = cache.setdefault(vname, {})
        scores: dict[str, dict[str, float]] = {}
        failures = 0
        for set_name, samples in sets.items():
            for s in samples:
                if s["id"] not in vcache:
                    r = llm_signal.score(s["text"], s["content_type"], **cfg)
                    vcache[s["id"]] = r.to_dict()
                    CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
                r = vcache[s["id"]]
                if not r["available"]:
                    failures += 1
                    continue
                scores.setdefault(set_name, {})[s["id"]] = r["ai_probability"]

        h, a, m = scores.get("human", {}), scores.get("ai", {}), scores.get("mixed", {})
        print(f"\n=== {vname}  (unavailable: {failures})")
        print(f"  mean p_llm  human={statistics.mean(h.values()):.2f}  ai={statistics.mean(a.values()):.2f}"
              f"  mixed={statistics.mean(m.values()):.2f}   AUC(human vs ai)={auc(list(a.values()), list(h.values())):.3f}")
        print(f"  human >= 0.80: {sum(v >= 0.8 for v in h.values())}/{len(h)}   ai <= 0.20: {sum(v <= 0.2 for v in a.values())}/{len(a)}")
        worst_h = sorted(h.items(), key=lambda kv: -kv[1])[:3]
        worst_a = sorted(a.items(), key=lambda kv: kv[1])[:3]
        print("  worst human (scored most AI): " + ", ".join(f"{k}={v:.2f}" for k, v in worst_h))
        print("  worst ai (scored most human): " + ", ".join(f"{k}={v:.2f}" for k, v in worst_a))


if __name__ == "__main__":
    main(sys.argv[1:])
