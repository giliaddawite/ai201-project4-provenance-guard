"""Run both signals and the combiner on a fixed set of texts and print a table.

Usage:  python scripts/probe_signals.py [--no-llm]

--no-llm skips Groq and shows stylometry only (fast, offline).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from provenance_guard import llm_signal, stylometry  # noqa: E402
from provenance_guard.combine import combine  # noqa: E402
from provenance_guard.labels import band  # noqa: E402

SAMPLES: dict[str, tuple[str, str]] = {
    # --- course-supplied Milestone 4 inputs --------------------------------
    "course: clearly AI": (
        "blog",
        "Artificial intelligence represents a transformative paradigm shift in modern society. "
        "It is important to note that while the benefits of AI are numerous, it is equally "
        "essential to consider the ethical implications. Furthermore, stakeholders across "
        "various sectors must collaborate to ensure responsible deployment.",
    ),
    "course: clearly human": (
        "blog",
        "ok so i finally tried that new ramen place downtown and honestly? "
        "underwhelming. the broth was fine but they put WAY too much sodium in it and "
        "i was thirsty for like three hours after. my friend got the spicy version and "
        "said it was better. probably won't go back unless someone drags me there",
    ),
    "course: borderline formal human": (
        "blog",
        "The relationship between monetary policy and asset price inflation has been "
        "extensively studied in the literature. Central banks face a fundamental tension "
        "between their mandate for price stability and the unintended consequences of "
        "prolonged low interest rates on equity and real estate valuations.",
    ),
    "course: borderline edited AI": (
        "blog",
        "I've been thinking a lot about remote work lately. There are genuine tradeoffs — "
        "flexibility and no commute on one side, isolation and blurred work-life boundaries "
        "on the other. Studies show productivity varies widely by individual and role type.",
    ),
    # --- Milestone 3 inputs, for signal-vs-signal comparison -----------------
    "m3: Melville": (
        "story",
        "Call me Ishmael. Some years ago—never mind how long precisely—having little or "
        "no money in my purse, and nothing particular to interest me on shore, I thought I "
        "would sail about a little and see the watery part of the world. It is a way I have "
        "of driving off the spleen and regulating the circulation. Whenever I find myself "
        "growing grim about the mouth; whenever it is a damp, drizzly November in my soul; "
        "whenever I find myself involuntarily pausing before coffin warehouses, and bringing "
        "up the rear of every funeral I meet; and especially whenever my hypos get such an "
        "upper hand of me, that it requires a strong moral principle to prevent me from "
        "deliberately stepping into the street, and methodically knocking people's hats "
        "off—then, I account it high time to get to sea as soon as I can.",
    ),
    "m3: generic blog": (
        "blog",
        "In today's fast-paced world, finding balance is more important than ever. Many "
        "people struggle to manage their time effectively, leading to stress and burnout. "
        "Fortunately, there are several strategies that can help. First, it is essential to "
        "prioritize your tasks and focus on what truly matters. Second, taking regular "
        "breaks can significantly improve productivity and mental clarity. Third, "
        "maintaining a healthy lifestyle through exercise and proper nutrition supports "
        "overall well-being. By implementing these simple yet effective techniques, you can "
        "create a more balanced and fulfilling life. Remember, small changes can lead to big "
        "results over time.",
    ),
    "m3: course curl (29w)": (
        "other",
        "The sun dipped below the horizon, painting the sky in hues of amber and rose. I "
        "sat on the porch, coffee in hand, watching the neighborhood slowly go quiet.",
    ),
}


def main(use_llm: bool) -> None:
    hdr = f"{'sample':34} {'words':>5} {'rel':>3} {'p_sty':>6} {'cv':>5} {'mattr':>5} {'punct':>5} {'claus':>5}"
    if use_llm:
        hdr += f" | {'p_llm':>5} {'p_ai':>5} {'conf':>5} {'d':>4} {'shr':>4} band"
    print(hdr)
    print("-" * len(hdr))
    for name, (ctype, text) in SAMPLES.items():
        s = stylometry.score(text)
        f = s.features
        line = (
            f"{name:34} {s.word_count:>5} {('y' if s.reliable else 'n'):>3} {s.ai_probability:>6.2f} "
            f"{f['sentence_len_cv']:>5.2f} {f['mattr']:>5.2f} {f['punct_per_100w']:>5.1f} {f['clause_markers_per_sent']:>5.2f}"
        )
        if use_llm:
            l = llm_signal.score(text, ctype)
            c = combine(l, s, s.word_count)
            p_llm = f"{l.ai_probability:>5.2f}" if l.available else "  n/a"
            line += (
                f" | {p_llm} {c.ai_probability:>5.2f} {c.confidence:>5.2f} {c.disagreement:>4.2f} "
                f"{c.shrink_factor:>4.2f} {band(c.ai_probability)}"
            )
        print(line)


if __name__ == "__main__":
    main(use_llm="--no-llm" not in sys.argv)
