"""Per-feature separation analysis for the stylometric signal (offline).

For each raw feature prints the human/AI means and medians and the AUC of
"higher value => AI". An AUC near 0.5 means the feature carries no signal on
this set; below 0.5 means it points the *opposite* way from the assumption.

    python calibration/feature_analysis.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provenance_guard import stylometry  # noqa: E402

HERE = Path(__file__).parent


def auc(pos: list[float], neg: list[float]) -> float:
    """Probability a random AI value exceeds a random human value (ties = 0.5)."""
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


def main() -> None:
    human = json.loads((HERE / "human.json").read_text(encoding="utf-8"))
    ai = json.loads((HERE / "ai.json").read_text(encoding="utf-8"))
    fh = [stylometry.features_for(s["text"])[0] for s in human]
    fa = [stylometry.features_for(s["text"])[0] for s in ai]

    print(f"{'feature':26} {'human mean':>10} {'human med':>9} {'ai mean':>8} {'ai med':>7} {'AUC(hi=>AI)':>12}")
    for k in stylometry.REFERENCE_RANGES:
        h = [f[k] for f in fh]
        a = [f[k] for f in fa]
        print(
            f"{k:26} {statistics.mean(h):>10.3f} {statistics.median(h):>9.3f} "
            f"{statistics.mean(a):>8.3f} {statistics.median(a):>7.3f} {auc(a, h):>12.3f}"
        )

    sh = [stylometry.score(s["text"]).ai_probability for s in human]
    sa = [stylometry.score(s["text"]).ai_probability for s in ai]
    print(f"\n{'p_stylo (current config)':26} {statistics.mean(sh):>10.3f} {statistics.median(sh):>9.3f} "
          f"{statistics.mean(sa):>8.3f} {statistics.median(sa):>7.3f} {auc(sa, sh):>12.3f}")


if __name__ == "__main__":
    main()
