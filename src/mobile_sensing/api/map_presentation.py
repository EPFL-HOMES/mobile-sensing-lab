"""Presentation-only color limits for bounded map and matrix projections."""

from collections.abc import Iterable
from math import isfinite
from numbers import Real
from typing import Literal

import numpy as np


def color_scale_metadata(
    values: Iterable[float],
    *,
    scope: Literal["complete_query", "returned_features"] = "complete_query",
) -> dict:
    """Summarize finite positive display values without modifying scientific data."""
    positive = np.fromiter(
        (value for value in values if isinstance(value, Real) and isfinite(value) and value > 0),
        dtype=np.float64,
    )
    return {
        "positive_p95": (
            float(np.quantile(positive, 0.95, method="linear")) if positive.size else None
        ),
        "maximum": float(positive.max()) if positive.size else 0.0,
        "positive_count": int(positive.size),
        "scope": scope,
    }
