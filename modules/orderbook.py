"""orderbook.py — Polymarket CLOB order book depth fetcher (Phase 13)

Phase 13 requires knowing how much capital can be deployed in a market
before slippage becomes material (1,000万円-scale orders).

API: https://clob.polymarket.com/book?token_id={token_id}
  token_id: YES-side CLOB token ID extracted from market['clobTokenIds'][0]

Usage in main loop:
    depth = fetch_depth(token_id)
    ev = calculate_effective_ev(yes_price, no_price,
             order_size=ORDER_SIZE_UNITS,
             liquidity_depth=depth.ask_depth if depth else None)
"""
import logging
import time
from typing import NamedTuple

import requests

CLOB_API        = "https://clob.polymarket.com"
DEPTH_TIMEOUT   = 4     # seconds — must not block the 15s main cycle
PRICE_WINDOW    = 0.05  # sum depths within ±5% of best price
STALE_BOOK_SECS = 5.0   # reject order book data older than this (real-money slippage risk)


class DepthSnapshot(NamedTuple):
    token_id:    str
    bid_depth:   float   # total available size on bid side near best
    ask_depth:   float   # total available size on ask side near best
    best_bid:    float   # highest bid price
    best_ask:    float   # lowest ask price
    spread:      float   # best_ask - best_bid
    levels_bid:  int     # number of bid price levels within window
    levels_ask:  int     # number of ask price levels within window
    book_age_ms: float = 0.0  # milliseconds since book was published (0 = no timestamp in response)

    def impact_cost_jpy(self, order_size_jpy: float,
                         price_per_share: float) -> float:
        """
        Estimate yen cost of market-impact slippage for an order.
        Assumes linear depth model: price moves proportionally to
        fraction of available liquidity consumed.

        Args:
            order_size_jpy:   Order size in JPY (e.g. 10_000_000)
            price_per_share:  Current YES price (e.g. 0.48)

        Returns:
            Extra cost in JPY due to slippage (0.0 if depth is sufficient)
        """
        if self.ask_depth <= 0 or price_per_share <= 0:
            return 0.0
        shares_needed    = order_size_jpy / price_per_share
        depth_fraction   = shares_needed / self.ask_depth
        slippage_per_jpy = depth_fraction * 0.001   # 0.1% per full depth consumed
        return order_size_jpy * slippage_per_jpy


def fetch_depth(token_id: str,
                price_window: float = PRICE_WINDOW) -> "DepthSnapshot | None":
    """
    Fetch CLOB order book and compute available depth near best price.

    Returns None on any API failure — caller falls back to no-depth EV.
    This must NEVER block the main scan cycle; hence DEPTH_TIMEOUT = 4s.
    """
    try:
        r = requests.get(f"{CLOB_API}/book",
                         params={"token_id": token_id},
                         timeout=DEPTH_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        # ── Freshness check: reject stale books before any calculation ─────────
        # Polymarket CLOB returns "timestamp" (ms epoch). If the book is older
        # than STALE_BOOK_SECS, slippage estimates are unreliable for real trades.
        book_age_ms = 0.0
        ts_raw = data.get("timestamp")
        if ts_raw is not None:
            try:
                book_age_ms = time.time() * 1000 - float(ts_raw)
                if book_age_ms > STALE_BOOK_SECS * 1000:
                    logging.info(
                        f"BOOK_STALE | token={token_id[:20]} | age={book_age_ms:.0f}ms "
                        f"(>{STALE_BOOK_SECS:.0f}s) — skipped"
                    )
                    return None
            except (ValueError, TypeError):
                pass  # malformed timestamp — proceed without freshness guard

        bids = [(float(b["price"]), float(b["size"]))
                for b in data.get("bids", []) if b.get("price") and b.get("size")]
        asks = [(float(a["price"]), float(a["size"]))
                for a in data.get("asks", []) if a.get("price") and a.get("size")]

        if not bids or not asks:
            return None

        best_bid = max(p for p, _ in bids)
        best_ask = min(p for p, _ in asks)

        # Sum liquidity within ±price_window of best price
        near_bids = [(p, s) for p, s in bids if p >= best_bid - price_window]
        near_asks = [(p, s) for p, s in asks if p <= best_ask + price_window]

        return DepthSnapshot(
            token_id    = token_id,
            bid_depth   = sum(s for _, s in near_bids),
            ask_depth   = sum(s for _, s in near_asks),
            best_bid    = best_bid,
            best_ask    = best_ask,
            spread      = best_ask - best_bid,
            levels_bid  = len(near_bids),
            levels_ask  = len(near_asks),
            book_age_ms = book_age_ms,
        )

    except requests.exceptions.Timeout:
        logging.info(f"DEPTH_TIMEOUT | token={token_id[:20]}")
        return None
    except Exception as e:
        logging.info(f"DEPTH_FAIL | token={token_id[:20]} | {e}")
        return None


def parse_clob_token_id(market: dict) -> "str | None":
    """
    Extract YES-side CLOB token ID from a Polymarket market dict.

    Gamma API returns clobTokenIds as a JSON-string list or a list:
        '["0xabc...", "0xdef..."]'  → YES token = index 0
        ["0xabc...", "0xdef..."]   → YES token = index 0
    """
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    try:
        import json
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return str(ids[0]) if ids else None
    except Exception:
        return None


def depth_summary(snap: DepthSnapshot, order_jpy: float,
                  yes_price: float) -> str:
    """One-line depth summary for terminal output."""
    impact   = snap.impact_cost_jpy(order_jpy, yes_price)
    age_str  = (f"  板鮮度={snap.book_age_ms:.0f}ms"
                if snap.book_age_ms > 0 else "")
    return (
        f"深さ ask={snap.ask_depth:.0f}shares  "
        f"spread={snap.spread:.4f}  "
        f"影響コスト¥{impact:,.0f}({order_jpy/1e4:.0f}万円注文時)"
        f"{age_str}"
    )
