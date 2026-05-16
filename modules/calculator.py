"""calculator.py — Position sizing (Kelly + scipy) & virtual trade simulation"""
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
