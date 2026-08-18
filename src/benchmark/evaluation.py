from __future__ import annotations

import math
from typing import Literal

EffectSizeType = Literal["relative", "absolute"]
Direction = Literal["increase", "decrease"]


def calculate_effect(
    baseline_value: float,
    fault_value: float,
    *,
    effect_size_type: EffectSizeType,
) -> float:
    """Return a signed absolute or relative change without dividing by zero."""
    baseline = float(baseline_value)
    fault = float(fault_value)
    if effect_size_type == "absolute":
        return fault - baseline
    if baseline == 0:
        if fault == 0:
            return 0.0
        return math.copysign(math.inf, fault)
    return (fault - baseline) / abs(baseline)


def validate_effect(
    baseline_value: float,
    fault_value: float,
    *,
    expected_direction: Direction,
    effect_size_type: EffectSizeType,
    minimum_effect_size: float,
) -> bool:
    """Validate direction and minimum magnitude for a concrete benchmark case."""
    if minimum_effect_size <= 0:
        raise ValueError("minimum_effect_size must be positive")
    effect = calculate_effect(
        baseline_value,
        fault_value,
        effect_size_type=effect_size_type,
    )
    if expected_direction == "increase":
        return effect >= minimum_effect_size
    if expected_direction == "decrease":
        return effect <= -minimum_effect_size
    raise ValueError(f"unsupported expected direction: {expected_direction}")
