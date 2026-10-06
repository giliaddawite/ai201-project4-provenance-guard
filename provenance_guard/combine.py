"""Combine the two signals into one calibrated AI probability.

This is a direct transcription of the pseudocode in planning.md,
"Combination into one score". Keep the two in sync.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .labels import AI_THRESHOLD, HUMAN_THRESHOLD
from .llm_signal import LlmResult
from .stylometry import StylometryResult

W_LLM_DEFAULT, W_STYLO_DEFAULT = 0.60, 0.40
W_LLM_STYLO_UNRELIABLE, W_STYLO_STYLO_UNRELIABLE = 0.85, 0.15

DISAGREEMENT_FREE_ZONE = 0.30   # |p_llm - p_stylo| up to this costs nothing
SHORT_TEXT_WORDS = 40           # below: shrink by 0.60
MEDIUM_TEXT_WORDS = 80          # below: shrink by 0.85
SHORT_SHRINK, MEDIUM_SHRINK = 0.60, 0.85
# A lone signal is clamped strictly inside the uncertain band, 0.05 clear of each
# threshold, so it can never produce a high-confidence label.
SINGLE_SIGNAL_FLOOR = round(HUMAN_THRESHOLD + 0.05, 4)   # 0.25
SINGLE_SIGNAL_CEIL = round(AI_THRESHOLD - 0.05, 4)       # 0.70


@dataclass
class CombinedResult:
    ai_probability: float
    confidence: float
    p_raw: float
    weights: dict
    disagreement: float
    shrink_factor: float
    single_signal_clamp: bool

    def to_dict(self) -> dict:
        return asdict(self)


def combine(llm: LlmResult, stylo: StylometryResult, word_count: int) -> CombinedResult:
    p_llm = llm.ai_probability if llm.available else 0.5
    p_stylo = stylo.ai_probability

    # Weights
    w_llm, w_stylo = W_LLM_DEFAULT, W_STYLO_DEFAULT
    if not stylo.reliable:
        w_llm, w_stylo = W_LLM_STYLO_UNRELIABLE, W_STYLO_STYLO_UNRELIABLE
    if not llm.available:
        w_llm, w_stylo = 0.0, 1.0

    p_raw = w_llm * p_llm + w_stylo * p_stylo

    # Disagreement shrink
    d = abs(p_llm - p_stylo)
    shrink = 1.0 if d <= DISAGREEMENT_FREE_ZONE else 1.0 - (d - DISAGREEMENT_FREE_ZONE)

    # Short-text shrink
    if word_count < SHORT_TEXT_WORDS:
        shrink *= SHORT_SHRINK
    elif word_count < MEDIUM_TEXT_WORDS:
        shrink *= MEDIUM_SHRINK

    p_ai = 0.5 + (p_raw - 0.5) * shrink

    # Single-signal cap
    clamped = False
    if not llm.available or not stylo.reliable:
        p_ai = min(SINGLE_SIGNAL_CEIL, max(SINGLE_SIGNAL_FLOOR, p_ai))
        clamped = True

    p_ai = round(min(1.0, max(0.0, p_ai)), 4)
    return CombinedResult(
        ai_probability=p_ai,
        confidence=round(max(p_ai, 1.0 - p_ai), 4),
        p_raw=round(p_raw, 4),
        weights={"llm": w_llm, "stylometry": w_stylo},
        disagreement=round(d, 4),
        shrink_factor=round(shrink, 4),
        single_signal_clamp=clamped,
    )
