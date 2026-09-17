#!/usr/bin/env python3
"""Wipe-out ("one coin / many coins go to zero") matrix for the g4 tail-risk study.

The parent study's engine caps every position: a slot may reach at most
`total_wallet_exposure_limit / n_positions * (1 + we_excess_allowance_pct)` of the balance,
and the book may reach at most `total_wallet_exposure_limit`. That is what makes the
""被一波带走"" question answerable with numbers:

* one coin going to zero costs at most that coin's peak wallet exposure,
* `k` coins going to zero together cost at most the sum of their peak exposures — but the
  engine never observes all coins at their own peak at the same time, so the *observed*
  bound is the run's `total_wallet_exposure_max`,
* the account's distance to ruin is therefore `1 - total_wallet_exposure_max` even in the
  limit where the whole loaded basket goes to zero.

Everything here is a pure function over local, regenerable run artifacts, so the report
renderer and the independent verifier compute the same matrix from the same inputs.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

#: Scopes the matrix reports, from the narrowest single-coin shock to the observed basket.
SCOPE_WORST_COIN = "worst_coin"
SCOPE_TOP3 = "top3_worst"
SCOPE_TOP7 = "top7_slots"
SCOPE_BASKET_PEAK = "basket_at_observed_peak"
SCOPE_ORDER = (SCOPE_WORST_COIN, SCOPE_TOP3, SCOPE_TOP7, SCOPE_BASKET_PEAK)
SCOPE_LABELS = {
    SCOPE_WORST_COIN: "暴露最大的单个币归零",
    SCOPE_TOP3: "暴露最大的 3 个币同时归零",
    SCOPE_TOP7: "全部 7 个槽位同时归零（峰值之和，上界）",
    SCOPE_BASKET_PEAK: "实测总暴露峰值处整篮归零",
}


@dataclass(frozen=True)
class WipeoutMatrix:
    """One arm's exposure bounds and the loss they imply for a set of shocks."""

    arm: str
    peak_total_exposure: float
    peak_exposure_balance_usd: float | None
    start_balance_usd: float | None
    per_coin: tuple[tuple[str, float], ...]
    shock_levels: tuple[float, ...]

    @property
    def sum_of_maxima(self) -> float:
        return float(sum(value for _coin, value in self.per_coin))

    def scope_exposure(self, scope: str) -> float:
        exposures = [value for _coin, value in self.per_coin]
        if scope == SCOPE_WORST_COIN:
            return float(exposures[0]) if exposures else 0.0
        if scope == SCOPE_TOP3:
            return float(sum(exposures[:3]))
        if scope == SCOPE_TOP7:
            return float(sum(exposures[:7]))
        if scope == SCOPE_BASKET_PEAK:
            return float(self.peak_total_exposure)
        raise ValueError(f"unknown scope {scope!r}")

    def loss_fraction(self, scope: str, shock: float) -> float:
        """Fraction of the account lost, capped at 100%."""
        return min(1.0, self.scope_exposure(scope) * float(shock))

    def loss_usd(self, scope: str, shock: float) -> float | None:
        if self.peak_exposure_balance_usd is None:
            return None
        return self.loss_fraction(scope, shock) * float(self.peak_exposure_balance_usd)

    @property
    def ruin_distance(self) -> float:
        """Equity left when the whole basket at its observed peak goes to zero."""
        return max(0.0, 1.0 - min(1.0, self.peak_total_exposure))

    def coins_to_breach(self, threshold: float) -> int | None:
        """How many of the worst coins must go to zero together to breach a loss threshold."""
        if threshold <= 0.0:
            return 0
        cumulative = 0.0
        for index, (_coin, value) in enumerate(self.per_coin, start=1):
            cumulative += value
            if cumulative >= threshold:
                return index
        return None

    def rows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for scope in SCOPE_ORDER:
            exposure = self.scope_exposure(scope)
            for shock in self.shock_levels:
                out.append(
                    {
                        "arm": self.arm,
                        "scope": scope,
                        "scope_label": SCOPE_LABELS[scope],
                        "shock": float(shock),
                        "exposure": exposure,
                        "loss_fraction": self.loss_fraction(scope, shock),
                        "loss_usd": self.loss_usd(scope, shock),
                    }
                )
        return out


def per_coin_max_exposure(fills: pd.DataFrame) -> list[tuple[str, float]]:
    """Per-coin maximum absolute wallet exposure, from the fill ledger itself.

    The engine writes the same quantity into `coin_metrics.csv`; the ledger is used here so
    the matrix stays independent of the report renderer, which rewrites that CSV.

    Hmm: the ledger samples exposure at fills, exactly like the engine's per-coin metric, so
    both are "maximum absolute wallet exposure at fill".
    """
    if fills.empty or "wallet_exposure" not in fills or "coin" not in fills:
        return []
    grouped = (
        fills.assign(_abs=lambda frame: frame["wallet_exposure"].abs())
        .groupby("coin")["_abs"]
        .max()
        .dropna()
    )
    return sorted(
        ((str(coin), float(value)) for coin, value in grouped.items()),
        key=lambda item: item[1],
        reverse=True,
    )


def per_coin_max_exposure_from_coin_metrics(coin_metrics: pd.DataFrame) -> list[tuple[str, float]]:
    """The engine's own per-coin exposure column, used as a cross-check."""
    if coin_metrics.empty or "max_abs_wallet_exposure_at_fill" not in coin_metrics:
        return []
    out: list[tuple[str, float]] = []
    for _, row in coin_metrics.iterrows():
        value = row.get("max_abs_wallet_exposure_at_fill")
        if value is None or not math.isfinite(float(value)):
            continue
        out.append((str(row.get("coin")), float(value)))
    return sorted(out, key=lambda item: item[1], reverse=True)


def build_matrix(
    arm: str,
    *,
    analysis: dict[str, Any],
    per_coin: Sequence[tuple[str, float]],
    fills: pd.DataFrame,
    shock_levels: Sequence[float],
    start_balance_usd: float | None = None,
) -> WipeoutMatrix:
    """Derive one arm's exposure bounds from its own tracked artifacts."""
    peak_total = float(analysis.get("total_wallet_exposure_max") or 0.0)
    ordered = sorted(((str(coin), float(value)) for coin, value in per_coin), key=lambda i: i[1], reverse=True)

    peak_balance: float | None = None
    if not fills.empty and {"twe_long", "usd_total_balance"} <= set(fills.columns):
        clean = fills.dropna(subset=["twe_long", "usd_total_balance"])
        if not clean.empty:
            row = clean.loc[clean["twe_long"].idxmax()]
            peak_balance = float(row["usd_total_balance"])
    return WipeoutMatrix(
        arm=arm,
        peak_total_exposure=peak_total,
        peak_exposure_balance_usd=peak_balance,
        start_balance_usd=start_balance_usd,
        per_coin=tuple(ordered),
        shock_levels=tuple(float(level) for level in shock_levels),
    )


def assert_identities(matrix: WipeoutMatrix) -> list[str]:
    """Structural invariants the matrix must satisfy; used by tests and the verifier."""
    problems: list[str] = []
    if matrix.peak_total_exposure > 1.0 + 1e-3:
        problems.append(
            f"{matrix.arm}: total_wallet_exposure_max {matrix.peak_total_exposure} exceeds 1.0 "
            "beyond the bounded excess allowance"
        )
    if not matrix.per_coin:
        problems.append(f"{matrix.arm}: no per-coin exposure recorded")
    ordered = [value for _coin, value in matrix.per_coin]
    if ordered != sorted(ordered, reverse=True):
        problems.append(f"{matrix.arm}: per-coin exposures are not sorted descending")
    if SCOPE_ORDER == (SCOPE_WORST_COIN, SCOPE_TOP3, SCOPE_TOP7, SCOPE_BASKET_PEAK):
        for index in range(1, len(matrix.shock_levels)):
            if matrix.shock_levels[index] <= matrix.shock_levels[index - 1]:
                problems.append(f"{matrix.arm}: shock levels are not strictly ascending")
                break
    for shock in matrix.shock_levels:
        values = [matrix.loss_fraction(scope, shock) for scope in SCOPE_ORDER[:3]]
        if values != sorted(values):
            problems.append(
                f"{matrix.arm}: loss is not monotonic in scope at shock {shock}: {values}"
            )
        for scope in SCOPE_ORDER:
            loss = matrix.loss_fraction(scope, shock)
            if not 0.0 <= loss <= 1.0:
                problems.append(f"{matrix.arm}: loss {loss} out of range for {scope}@{shock}")
            if scope != SCOPE_BASKET_PEAK and loss > shock * matrix.sum_of_maxima + 1e-9:
                problems.append(
                    f"{matrix.arm}: {scope}@{shock} exceeds the sum-of-maxima bound"
                )
    if matrix.ruin_distance < 0.0:
        problems.append(f"{matrix.arm}: ruin distance is negative")
    return problems


def matrix_rows(matrices: Iterable[WipeoutMatrix]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for matrix in matrices:
        rows.extend(matrix.rows())
    return rows


def ruin_summary_rows(matrices: Iterable[WipeoutMatrix]) -> list[dict[str, Any]]:
    """One row per arm: the headline numbers the report leads with."""
    rows: list[dict[str, Any]] = []
    for matrix in matrices:
        rows.append(
            {
                "arm": matrix.arm,
                "peak_total_exposure": matrix.peak_total_exposure,
                "ruin_distance": matrix.ruin_distance,
                "worst_coin": matrix.per_coin[0][0] if matrix.per_coin else None,
                "worst_coin_exposure": matrix.per_coin[0][1] if matrix.per_coin else None,
                "top3_exposure": matrix.scope_exposure(SCOPE_TOP3),
                "top7_exposure": matrix.scope_exposure(SCOPE_TOP7),
                "sum_of_maxima": matrix.sum_of_maxima,
                "peak_exposure_balance_usd": matrix.peak_exposure_balance_usd,
                "worst_coin_zero_loss_usd": matrix.loss_usd(SCOPE_WORST_COIN, 1.0),
                "basket_zero_loss_usd": matrix.loss_usd(SCOPE_BASKET_PEAK, 1.0),
                "coins_to_breach_20pct": matrix.coins_to_breach(0.20),
                "coins_to_breach_50pct": matrix.coins_to_breach(0.50),
                "coins_to_breach_80pct": matrix.coins_to_breach(0.80),
            }
        )
    return rows


def write_csv(rows: Sequence[dict[str, Any]], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(list(rows)).to_csv(path, index=False)
    return path
