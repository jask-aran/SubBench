"""Solve for how much quota each model consumes per token.

Quota-per-dollar varies several-fold inside a single window. The likeliest cause is model
mix: a provider's meter is not denominated in API dollars, so a token of one model can
cost a different share of the allowance than a token of another at the same price. The
window estimate averages that variation away; these weights explain it instead.

The model is linear. For window i with T[i][m] tokens of model m and Q[i] percent of the
allowance consumed:

    Q[i] = sum_m w[m] * T[i][m]

Solved by non-negative least squares -- negative weights would mean a model refunded
quota. Weights are per provider, since two providers' meters are unrelated.

The fit is gated. With fewer independent windows than unknowns the system is
underdetermined and least squares still returns an answer, one that looks authoritative
and is noise. `solve` reports insufficiency rather than guessing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# Each model is an unknown. Requiring a margin of windows beyond that leaves the system
# overdetermined enough that one unusual window cannot dictate a weight.
MIN_WINDOW_MARGIN = 3

# Windows whose mixes are near-identical carry the same equation repeatedly and add no
# information. A model must vary by at least this share of tokens across windows to be
# separable from the others.
MIN_MIX_VARIATION = 0.05

MAX_ITERATIONS = 500
CONVERGENCE = 1e-10


@dataclass(frozen=True)
class WeightFit:
    provider: str
    models: list[str]
    weights: list[float]
    window_count: int
    residual: float
    sufficient: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "window_count": self.window_count,
            "residual": self.residual,
            "sufficient": self.sufficient,
            "reason": self.reason,
            "weights": [
                {"model": model, "quota_percent_per_million_tokens": weight * 1e6}
                for model, weight in zip(self.models, self.weights)
            ],
        }


def _solve_nnls(rows: list[list[float]], targets: list[float], columns: int) -> tuple[list[float], float]:
    """Non-negative least squares by projected gradient descent.

    Pure standard library on purpose: this runs inside the estimator package, which has no
    third-party dependencies and must keep running on the Workers runtime.
    """
    weights = [0.0] * columns
    # Normal equations: A'A w = A'b, descended rather than inverted so the non-negativity
    # constraint can be applied at every step.
    ata = [[sum(row[i] * row[j] for row in rows) for j in range(columns)] for i in range(columns)]
    atb = [sum(row[i] * target for row, target in zip(rows, targets)) for i in range(columns)]
    scale = max((ata[i][i] for i in range(columns)), default=0.0)
    if scale <= 0:
        return weights, float("inf")
    step = 1.0 / (scale * columns)

    for _ in range(MAX_ITERATIONS):
        moved = 0.0
        for i in range(columns):
            gradient = sum(ata[i][j] * weights[j] for j in range(columns)) - atb[i]
            updated = max(0.0, weights[i] - step * gradient)
            moved = max(moved, abs(updated - weights[i]))
            weights[i] = updated
        if moved < CONVERGENCE:
            break

    residual = 0.0
    for row, target in zip(rows, targets):
        predicted = sum(row[i] * weights[i] for i in range(columns))
        residual += (predicted - target) ** 2
    return weights, residual ** 0.5


def solve(observations: Iterable[Mapping[str, Any]], *, provider: str) -> WeightFit:
    """Fit per-model quota weights from windows of one provider.

    Each observation needs `quota_percent` (allowance consumed in that window) and
    `tokens` (a model to token-count mapping).
    """
    windows = [row for row in observations if str(row.get("provider")) == provider]
    models = sorted({model for row in windows for model in dict(row["tokens"])})
    if not models:
        return WeightFit(provider, [], [], 0, 0.0, False, "no model token counts recorded")

    required = len(models) + MIN_WINDOW_MARGIN
    if len(windows) < required:
        return WeightFit(
            provider, models, [], len(windows), 0.0, False,
            f"{len(windows)} windows for {len(models)} models, need {required}",
        )

    rows: list[list[float]] = []
    targets: list[float] = []
    for row in windows:
        tokens = dict(row["tokens"])
        total = sum(float(value) for value in tokens.values())
        if total <= 0 or float(row.get("quota_percent", 0)) <= 0:
            continue
        rows.append([float(tokens.get(model, 0)) for model in models])
        targets.append(float(row["quota_percent"]))

    if len(rows) < required:
        return WeightFit(
            provider, models, [], len(rows), 0.0, False,
            f"{len(rows)} usable windows for {len(models)} models, need {required}",
        )

    shares = [[row[i] / sum(row) if sum(row) else 0.0 for i in range(len(models))] for row in rows]
    for index, model in enumerate(models):
        column = [share[index] for share in shares]
        if max(column) - min(column) < MIN_MIX_VARIATION:
            # Every window used this model in the same proportion, so its contribution
            # cannot be told apart from the constant term.
            return WeightFit(
                provider, models, [], len(rows), 0.0, False,
                f"{model} share varies by less than {MIN_MIX_VARIATION:.0%} across windows",
            )

    weights, residual = _solve_nnls(rows, targets, len(models))
    return WeightFit(provider, models, weights, len(rows), residual, True, "fitted")


def observations_from_windows(
    estimates: Iterable[Any], mix: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Join per-window quota movement to that window's model token counts."""
    # Mix rows carry the raw reset timestamp, which wanders by seconds between reads and
    # can straddle a minute. Estimates are keyed on the clustered value, so cluster here
    # too or the fragments simply fail to join and the fit silently sees fewer windows.
    from .regression import _cluster_resets, _reset_key

    rows = [dict(row) for row in mix]
    by_series: dict[tuple[str, str | None], list[str]] = {}
    for row in rows:
        key = _reset_key(row.get("resets_at"))
        row["_reset_key"] = key
        by_series.setdefault((str(row["provider"]), row.get("account_id")), []).append(key)
    clusters = {series: _cluster_resets(keys) for series, keys in by_series.items()}

    tokens: dict[tuple[str, str | None, str], dict[str, float]] = {}
    for row in rows:
        series = (str(row["provider"]), row.get("account_id"))
        key = (series[0], series[1], clusters[series][row["_reset_key"]])
        model = str(row["model"])
        # Two agents' rows for one window are separate observations of the same tokens
        # only if they are the same machine; take the larger rather than summing, since
        # a window's mix is a proportion and double counting would not change it anyway.
        current = tokens.setdefault(key, {})
        current[model] = max(current.get(model, 0.0), float(row["total_tokens"]))

    observations = []
    for estimate in estimates:
        key = (estimate.provider, estimate.account_id, estimate.reset_key)
        counts = tokens.get(key)
        if not counts:
            continue
        observations.append({
            "provider": estimate.provider,
            "account_id": estimate.account_id,
            "reset_key": estimate.reset_key,
            "quota_percent": estimate.covered_quota_percent,
            "tokens": counts,
        })
    return observations
