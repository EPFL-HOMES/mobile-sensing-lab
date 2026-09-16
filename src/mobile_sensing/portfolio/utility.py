"""The same pointwise utility is used by sampling and the authoring curve."""

import math


def pointwise_utility(duration_s, kind, saturation_s):
    if (
        not math.isfinite(duration_s)
        or duration_s < 0
        or not math.isfinite(saturation_s)
        or saturation_s <= 0
    ):
        raise ValueError("Utility needs finite nonnegative exposure and positive saturation")
    if kind in {"exponential", "exponential_saturation"}:
        return -math.expm1(-duration_s / saturation_s)
    if kind == "linear_capped":
        return min(duration_s / saturation_s, 1.0)
    if kind == "binary":
        return float(duration_s > 0)
    if kind == "linear_diagnostic":
        return duration_s / saturation_s
    raise ValueError(f"Unsupported utility {kind!r}")
