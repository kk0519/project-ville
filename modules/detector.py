"""detector.py — Edge detection, logical inconsistency, triangle arb, event cluster scan"""
from dataclasses import dataclass
from itertools import combinations, permutations

POLYMARKET_FEE = 0.02   # 2% standard fee


@dataclass
class MarketData:
    question:  str
    market_id: str
    yes_price: float
    no_price:  float

    @property
    def total(self) -> float:
        return self.yes_price + self.no_price

    @property
    def edge(self) -> float:
        return self.total - 1.0


def detect_edge(md: MarketData, threshold: float = 0.02) -> bool:
    """True if YES+NO sum <= (1 - threshold) => potential arb"""
    return md.total <= (1.0 - threshold)


def calculate_ev(yes_price: float, no_price: float,
                 fee: float = POLYMARKET_FEE) -> dict:
    """Spread-adjusted expected value per unit capital."""
    results = {}
    for side, price in (("YES", yes_price), ("NO", no_price)):
        cost    = price + fee / 2          # half-spread entry cost
        net_ev  = (1.0 - cost)             # profit if win
        ev_pct  = net_ev / cost * 100
        results[side] = {"cost": cost, "net_ev": net_ev, "ev_pct": ev_pct}

    best = max(results, key=lambda s: results[s]["net_ev"])
    return {
        "YES_ev":    results["YES"]["net_ev"],
        "YES_ev_pct": results["YES"]["ev_pct"],
        "NO_ev":     results["NO"]["net_ev"],
        "NO_ev_pct": results["NO"]["ev_pct"],
        "best_side": best,
        "best_ev":   results[best]["net_ev"],
        "best_ev_pct": results[best]["ev_pct"],
    }


def scan_triangle_arb(markets: list["MarketData"],
                       min_compound: float = 0.005,
                       related_id_pairs: "set[frozenset[str]] | None" = None) -> list[dict]:
    """
    Find 3-market chains with compound monotonicity violations.

    Requirements for a valid triangle:
    1. ALL three pairs (A-B, B-C, A-C) must be in related_id_pairs
       (logically dependent markets identified by ai_analyst).
       If related_id_pairs is None or empty, no triangles are returned.
    2. Net profit must exceed min_compound AFTER deducting 2 * POLYMARKET_FEE
       (one fee per leg: AB leg and BC leg).

    Payoff analysis for chain C→B→A (C implies B implies A):
      - BUY A_yes @ P_a, BUY B_no @ (1-P_b): riskless profit = P_b - P_a - fee
      - BUY B_yes @ P_b, BUY C_no @ (1-P_c): riskless profit = P_c - P_b - fee
      - Combined net (2 legs): (P_c - P_a) - 2*fee, guaranteed in all outcomes.
    """
    if not related_id_pairs:
        return []

    found: list[dict] = []
    seen: set[frozenset] = set()
    FEE_TWO_LEGS = POLYMARKET_FEE * 2   # 2% × 2 legs = 4% total fee

    for triple in combinations(markets, 3):
        key = frozenset(m.market_id for m in triple)
        if key in seen:
            continue

        # All three pairs must be logically related
        ids = [m.market_id for m in triple]
        if not (
            frozenset([ids[0], ids[1]]) in related_id_pairs and
            frozenset([ids[1], ids[2]]) in related_id_pairs and
            frozenset([ids[0], ids[2]]) in related_id_pairs
        ):
            continue

        best_net = 0.0
        best_cfg: dict | None = None

        for perm in permutations(triple):
            a, b, c = perm
            ab = b.yes_price - a.yes_price   # >0 = monotonicity violation
            bc = c.yes_price - b.yes_price   # >0 = monotonicity violation
            if ab > 0 and bc > 0:
                net = (ab + bc) - FEE_TWO_LEGS
                if net > best_net:
                    best_net = net
                    best_cfg = {"a": a, "b": b, "c": c,
                                "ab": ab, "bc": bc, "gross": ab + bc}

        if best_cfg and best_net >= min_compound:
            seen.add(key)
            a, b, c = best_cfg["a"], best_cfg["b"], best_cfg["c"]
            found.append({
                "type":            "TRIANGLE_ARB",
                "chain":           [a.question[:50], b.question[:50], c.question[:50]],
                "prices":          [a.yes_price, b.yes_price, c.yes_price],
                "compound_profit": best_net,        # net after fees
                "gross_profit":    best_cfg["gross"],
                "ab_profit":       best_cfg["ab"],
                "bc_profit":       best_cfg["bc"],
                "strategy": (
                    f"[{a.question[:25]}]@{a.yes_price:.4f}→"
                    f"[{b.question[:25]}]@{b.yes_price:.4f}→"
                    f"[{c.question[:25]}]@{c.yes_price:.4f} "
                    f"純利益={best_net:.4f}(手数料{FEE_TWO_LEGS:.0%}控除後)"
                ),
            })

    return sorted(found, key=lambda x: x["compound_profit"], reverse=True)


def scan_event_cluster(markets: list["MarketData"],
                        keyword: str) -> list[dict]:
    """
    Filter markets containing keyword, then exhaustive all-pair arb scan.
    More thorough than adjacent-only: checks every combination in the cluster.
    """
    cluster = [m for m in markets if keyword.lower() in m.question.lower()]
    if len(cluster) < 2:
        return []

    results: list[dict] = []
    for a, b in combinations(cluster, 2):
        # Check both orderings to catch any direction of mispricing
        for ma, mb in ((a, b), (b, a)):
            arb = check_logical_inconsistency(ma, mb)
            if arb:
                arb["event_keyword"] = keyword
                arb["cluster_size"]  = len(cluster)
                results.append(arb)
                break
    return results


def check_logical_inconsistency(market_a: MarketData,
                                 market_b: MarketData) -> dict | None:
    """
    If B logically implies A (e.g., BTC>$80k implies BTC>$70k),
    then P(A_yes) >= P(B_yes) must hold.

    Strategy: BUY A_yes @ P_a  +  BUY B_no @ (1 - P_b)
      - Total cost: P_a + (1 - P_b)
      - Payout in all valid outcomes: at least 1.0
      - Gross profit/unit: P_b - P_a
      - Net after 2% Polymarket fee: (P_b - P_a) - POLYMARKET_FEE

    Only called for AI-identified logically related pairs.
    Returns None if net profit <= 0 after fees.
    """
    if market_b.yes_price > market_a.yes_price:
        gross = market_b.yes_price - market_a.yes_price
        net   = gross - POLYMARKET_FEE
        if net <= 0:
            return None   # fee wipes out the edge
        return {
            "type":            "LOGICAL_INCONSISTENCY",
            "market_a":        market_a.question,
            "market_b":        market_b.question,
            "a_yes":           market_a.yes_price,
            "b_yes":           market_b.yes_price,
            "gross_profit":    gross,
            "arb_profit_unit": net,
            "strategy": (
                f"BUY [{market_a.question[:35]}] YES @ {market_a.yes_price:.4f} | "
                f"BUY [{market_b.question[:35]}] NO @ {1-market_b.yes_price:.4f} | "
                f"純利益/unit = {net:.4f}(手数料2%控除後)"
            ),
        }
    return None
