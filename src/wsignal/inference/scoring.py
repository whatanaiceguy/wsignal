import math
from typing import Literal

SignalClass = Literal["weak", "strong", "noise"]


def compute_entry_score(
    substance: float,
    momentum: float,
    substance_weight: float = 0.75,
    momentum_weight: float = 0.25,
) -> float:
    score = substance_weight * substance + momentum_weight * momentum
    return round(min(1.0, max(0.0, score)), 3)


def compute_weak_score(
    substance: float,
    momentum: float,
    faintness: float,
    gate_centre: float = 0.275,
    gate_width: float = 0.05,
) -> float:
    base_score = (substance**0.5) * (faintness + momentum) / 2
    gate = 1 / (1 + math.exp(-(faintness - gate_centre) / gate_width))
    score = base_score * gate
    return round(min(1.0, max(0.0, score)), 3)


def classify_signal(
    substance: float,
    faintness: float,
    noise_below: float = 0.25,
    faintness_gate_centre: float = 0.275,
    strong_substance_at_least: float = 0.6,
) -> SignalClass:
    if substance < noise_below:
        return "noise"
    if faintness < faintness_gate_centre and substance >= strong_substance_at_least:
        return "strong"
    return "weak"


def entry_ranking_key(entry: object) -> tuple[bool, float, float]:
    weak_score = getattr(entry, "weak_score", None)
    score = float(entry.score)
    return weak_score is None, -(weak_score if weak_score is not None else 0.0), -score


def entry_ordering(weak_score, score, identity):
    return weak_score.desc().nulls_last(), score.desc(), identity
