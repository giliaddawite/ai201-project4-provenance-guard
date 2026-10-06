"""Post a fixed set of texts to a running Provenance Guard server and print a table.

    python scripts/demo_submit.py [base_url]      # default http://localhost:5000

Uses only the standard library so it can run outside the project venv. Mind the
rate limit (5 submissions per minute per IP): the default set is four texts.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:5000"

SAMPLES = [
    ("clearly AI (course)", "blog", "demo-ai",
     "Artificial intelligence represents a transformative paradigm shift in modern society. "
     "It is important to note that while the benefits of AI are numerous, it is equally "
     "essential to consider the ethical implications. Furthermore, stakeholders across "
     "various sectors must collaborate to ensure responsible deployment."),
    ("clearly human (course)", "blog", "demo-human",
     "ok so i finally tried that new ramen place downtown and honestly? "
     "underwhelming. the broth was fine but they put WAY too much sodium in it and "
     "i was thirsty for like three hours after. my friend got the spicy version and "
     "said it was better. probably won't go back unless someone drags me there"),
    ("borderline: formal human (course)", "blog", "demo-formal",
     "The relationship between monetary policy and asset price inflation has been "
     "extensively studied in the literature. Central banks face a fundamental tension "
     "between their mandate for price stability and the unintended consequences of "
     "prolonged low interest rates on equity and real estate valuations."),
    ("borderline: edited AI (course)", "blog", "demo-edited",
     "I've been thinking a lot about remote work lately. There are genuine tradeoffs — "
     "flexibility and no commute on one side, isolation and blurred work-life boundaries "
     "on the other. Studies show productivity varies widely by individual and role type."),
]


def post(path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def main() -> None:
    print(f"{'sample':34} {'status':>6} {'p_llm':>5} {'p_sty':>5} {'p_ai':>5} {'conf':>5} {'label':12} content_id")
    print("-" * 110)
    for name, ctype, creator, text in SAMPLES:
        code, d = post("/submit", {"creator_id": creator, "content_type": ctype, "text": text})
        if code != 201:
            print(f"{name:34} {code:>6} {d.get('error')}: {d.get('message')}")
            continue
        llm = d["signals"]["llm"]
        p_llm = f"{llm['ai_probability']:.2f}" if llm["available"] else "n/a"
        print(
            f"{name:34} {code:>6} {p_llm:>5} {d['signals']['stylometry']['ai_probability']:>5.2f} "
            f"{d['ai_probability']:>5.2f} {d['confidence']:>5.2f} {d['label']['variant']:12} {d['content_id']}"
        )


if __name__ == "__main__":
    main()
