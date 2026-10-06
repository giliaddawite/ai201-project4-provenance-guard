"""Generate the AI half of the calibration set with Groq and save it to ai.json.

Prompts vary genre, register and length, and several explicitly ask the model
to sound human, so the set is not just "default assistant voice". Run once:

    python calibration/generate_ai.py

Re-running fills in only empty samples; pass --force to regenerate everything. The generated texts are committed so the
calibration numbers in the README are reproducible without an API key.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from groq import Groq  # noqa: E402

load_dotenv()

OUT = Path(__file__).with_name("ai.json")
MODEL = os.environ.get("GROQ_GEN_MODEL", "openai/gpt-oss-120b")

PROMPTS: list[tuple[str, str, float]] = [
    # id, prompt, temperature
    ("a01_blog_productivity", "Write a 150-word blog post about staying productive while working from home.", 0.7),
    ("a02_blog_travel", "Write a 160-word travel blog paragraph about visiting Lisbon for the first time.", 0.9),
    ("a03_story_lighthouse", "Write the 170-word opening of a short story about a lighthouse keeper who finds a letter.", 0.9),
    ("a04_story_detective", "Write a 160-word noir detective scene set in a rainy city.", 1.0),
    ("a05_poem_autumn", "Write a free-verse poem of about 14 lines about autumn in a small town.", 0.9),
    ("a06_poem_villanelle", "Write a villanelle about insomnia. Follow the form strictly.", 0.8),
    ("a07_blog_human_voice", "Write a 150-word casual blog post about your first week learning to cook. Sound like a real person: vary your sentence length a lot, use a fragment or two, one aside in parentheses, and no bullet points.", 1.0),
    ("a08_blog_lowercase", "Write a 120-word social-media style post reviewing a coffee shop. all lowercase, casual, like texting a friend, with one 'honestly?' and one 'anyway'.", 1.0),
    ("a09_story_child_voice", "Write a 150-word story excerpt narrated by a nine-year-old about losing a tooth. Keep the vocabulary simple and the sentences short.", 0.9),
    ("a10_essay_ai_ethics", "Write a 150-word paragraph on the ethical implications of artificial intelligence for society.", 0.5),
    ("a11_blog_ramen_review", "Write a 110-word honest, slightly disappointed review of a new ramen restaurant, in the voice of a twenty-something blogger.", 1.0),
    ("a12_story_grandmother", "Write a 180-word memoir-style passage about a grandmother's kitchen. Make it feel specific and personal, with a concrete detail or two that only a family member would know.", 0.9),
    ("a13_poem_haiku_sequence", "Write a sequence of five haiku about a commuter train. Put each haiku on three lines with a blank line between haiku.", 0.9),
    ("a14_blog_remote_work", "Write a 140-word reflective blog paragraph about the tradeoffs of remote work. Use an em dash somewhere and avoid lists.", 0.8),
    ("a15_story_sea", "Write the 170-word opening of a novel narrated by a sailor who has decided to go to sea again. Use long, winding nineteenth-century sentences with semicolons.", 0.9),
]

SYSTEM = (
    "You are a writing model producing sample texts for a research dataset. "
    "Return only the requested text, with no title, preamble, or commentary."
)


def generate(client: Groq, prompt: str, temp: float) -> str:
    """One generation. Reasoning models can spend the whole budget thinking, so
    keep reasoning effort low, allow a generous budget, and retry once if empty."""
    for attempt in range(2):
        resp = client.chat.completions.create(
            model=MODEL,
            temperature=temp,
            max_tokens=4000,
            extra_body={"reasoning_effort": "low"},
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        )
        text = (resp.choices[0].message.content or "").strip()
        if text:
            return text
    return ""


def main() -> None:
    client = Groq(timeout=90.0)
    existing = {s["id"]: s for s in json.loads(OUT.read_text(encoding="utf-8"))} if OUT.exists() else {}
    force = "--force" in sys.argv
    samples = []
    for sid, prompt, temp in PROMPTS:
        if not force and existing.get(sid, {}).get("text"):
            samples.append(existing[sid])
            print(f"{sid:26} {len(existing[sid]['text'].split()):>4} words (kept)")
            continue
        text = generate(client, prompt, temp)
        ctype = "poem" if "poem" in sid else "story" if "story" in sid else "blog"
        samples.append(
            {"id": sid, "source": f"Groq {MODEL}, temperature {temp}: {prompt}", "content_type": ctype, "text": text}
        )
        print(f"{sid:26} {len(text.split()):>4} words")
    OUT.write_text(json.dumps(samples, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {len(samples)} samples to {OUT}")


if __name__ == "__main__":
    main()
