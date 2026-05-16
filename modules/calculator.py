"""calculator.py — Position sizing (Kelly + scipy), virtual trade simulation,
                   and Phase 13 multi-account capital distribution.
"""
POLYMARKET_FEE = 0.02

try:
    from scipy.optimize import minimize
    _SCIPY = True
except ImportError:
    _SCIPY = False


def simulate_trade(yes_price: float, no_price: float,
                   capital: float = 100_000, side: str = "YES",
                   fee_rate: float = POLYMARKET_FEE) -> dict:
    """
    Virtual trade simulator (no real orders).
    Returns P&L breakdown for a hypothetical 10万円 entry.
    """
    entry  = yes_price if side == "YES" else no_price
    fee    = capital * fee_rate
    net_in = capital - fee
    shares = net_in / entry

    win_gross = shares * 1.0
    win_net   = win_gross - capital
    lose_net  = -capital
    ev        = win_net * entry + lose_net * (1.0 - entry)

    return {
        "side":           side,
        "capital":        capital,
        "fee":            fee,
        "entry_price":    entry,
        "shares":         shares,
        "win_net_pnl":    win_net,
        "lose_net_pnl":   lose_net,
        "expected_value": ev,
        "roi_if_win_pct": win_net / capital * 100,
    }


def optimal_position_size(yes_price: float, capital: float = 100_000,
                           max_dd_pct: float = 0.10,
                           fee_rate: float = POLYMARKET_FEE) -> dict:
    """Kelly criterion (half-Kelly) with max drawdown cap."""
    p   = yes_price
    b   = (1.0 / yes_price) - 1.0 - fee_rate  # net odds after fee

    if b <= 0 or p <= 0:
        return {
            "kelly_f": 0.0, "half_kelly_f": 0.0,
            "recommended_size": 0.0,
            "recommended_size_jpy": "¥0",
            "reason": "Negative edge after fees",
        }

    kelly_f      = max(0.0, min((b * p - (1 - p)) / b, 1.0))
    half_kelly_f = kelly_f * 0.5
    max_cap      = capital * max_dd_pct
    size         = min(capital * half_kelly_f, max_cap)

    result = {
        "kelly_f":             kelly_f,
        "half_kelly_f":        half_kelly_f,
        "recommended_size":    size,
        "recommended_size_jpy": f"¥{size:,.0f}",
        "max_dd_limit_jpy":    f"¥{max_cap:,.0f}",
    }

    if _SCIPY:
        def neg_ev(x):
            bet = x[0]
            net = bet * (1 - fee_rate)
            shares = net / yes_price
            win_pnl  = shares - bet
            lose_pnl = -bet
            return -(win_pnl * p + lose_pnl * (1 - p))

        res = minimize(neg_ev, x0=[size],
                       bounds=[(0, capital)],
                       constraints=[{"type": "ineq", "fun": lambda x: max_cap - x[0]}])
        if res.success:
            result["scipy_optimal_jpy"] = f"¥{res.x[0]:,.0f}"

    return result


# ── Phase 13: Multi-account capital distribution ──────────────────

def distribute_capital(
    total_capital:    float,
    positions:        list[dict],
    num_accounts:     int   = 3,
    max_per_account:  float = 5_000_000,
    min_ev:           float = 0.005,
    fee_rate:         float = POLYMARKET_FEE,
) -> list[dict]:
    """
    Proportional-Kelly capital distribution across multiple accounts.

    For 1,000万円-scale operations, concentrating capital in a single
    Polymarket account creates slippage and single-point-of-failure risk.
    This function distributes capital across `num_accounts` accounts
    using Kelly-weighted proportional allocation.

    Algorithm:
        1. Filter positions with ev >= min_ev (quality gate)
        2. Kelly fractions are normalised to sum ≤ 1.0 (portfolio Kelly)
           to avoid simultaneous over-betting across correlated markets.
        3. Allocate proportionally; round-robin across accounts.
        4. Each account is capped at max_per_account.

    Args:
        total_capital:   Total deployable capital in JPY (e.g. 10_000_000)
        positions:       List of dicts with keys:
                           market_id   str
                           best_side   str  "YES" | "NO"
                           yes_price   float
                           kelly_f     float  (from optimal_position_size)
                           ev          float  (expected value per unit)
                           question    str    (for logging)
        num_accounts:    Number of accounts to spread across (default 3)
        max_per_account: Hard cap per account in JPY (default 500万円)
        min_ev:          Minimum EV threshold to include a position
        fee_rate:        Fee per leg (default 2%)

    Returns:
        List of allocation dicts:
          {market_id, account_idx, capital_jpy, kelly_weight,
           ev, best_side, question, impact_warning}
    """
    eligible = [p for p in positions
                if p.get("ev", 0) >= min_ev and p.get("kelly_f", 0) > 0]

    if not eligible:
        return []

    # Normalise Kelly fractions (portfolio Kelly: sum → 1.0)
    total_kelly = sum(p["kelly_f"] for p in eligible)
    allocations = []
    remaining   = total_capital

    for i, pos in enumerate(eligible):
        if remaining <= 0:
            break

        norm_weight = pos["kelly_f"] / total_kelly   # proportional share
        raw_alloc   = total_capital * norm_weight
        account_idx = i % num_accounts               # round-robin assignment
        alloc       = min(raw_alloc, max_per_account, remaining)

        if alloc < 1000:   # skip sub-¥1,000 slices (transaction overhead)
            continue

        impact_warning = alloc > 1_000_000   # flag >100万円 single allocation

        allocations.append({
            "market_id":      pos.get("market_id", ""),
            "question":       pos.get("question", "")[:60],
            "best_side":      pos.get("best_side", "YES"),
            "account_idx":    account_idx,
            "capital_jpy":    round(alloc),
            "kelly_weight":   round(norm_weight, 4),
            "ev":             pos.get("ev", 0.0),
            "impact_warning": impact_warning,
        })
        remaining -= alloc

    return sorted(allocations, key=lambda x: x["capital_jpy"], reverse=True)


def distribution_summary(allocations: list[dict]) -> str:
    """One-line summary for terminal/log output."""
    if not allocations:
        return "配分なし"
    total = sum(a["capital_jpy"] for a in allocations)
    acct_totals = {}
    for a in allocations:
        acct_totals[a["account_idx"]] = (
            acct_totals.get(a["account_idx"], 0) + a["capital_jpy"]
        )
    acct_str = "  ".join(
        f"口座{k}:¥{v:,.0f}" for k, v in sorted(acct_totals.items())
    )
    return f"配分{len(allocations)}件  合計¥{total:,.0f}  [{acct_str}]"
