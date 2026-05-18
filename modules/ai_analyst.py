"""ai_analyst.py — DeepSeek-R1 API: AI-driven logical market pair detection"""
import json
import logging
import os
import time
import requests

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL   = "deepseek-chat"   # swap to "deepseek-reasoner" for R1
TIMEOUT_SEC      = 30
MAX_RETRIES      = 3

_SYSTEM_PROMPT = """You are a prediction market analyst specializing in logical dependencies.
Given a list of market questions, identify pairs where one market LOGICALLY IMPLIES another.
Example: "Will BTC exceed $80k?" logically implies "Will BTC exceed $70k?" must also be true.
Return ONLY a valid JSON array. No explanation outside the JSON."""

_USER_TEMPLATE = """Analyze these prediction market questions for STRICT logical dependencies.

{markets}

STRICT rule: only include pairs where B MATHEMATICALLY GUARANTEES A is true.
CRITICAL DIRECTION RULE:
  market_a = the BROADER/EASIER market (implied by B). It has a LATER deadline OR LOWER threshold.
  market_b = the NARROWER/HARDER market (the implicant). It has an EARLIER deadline OR HIGHER threshold.

VALID strict implications (correct direction):
- A="BTC > $76k on May 18"  B="BTC > $82k on May 18"  (B harder: higher threshold, same date)
- A="Score > 205.5"          B="Score > 210.5"          (B harder: higher O/U line)
- A="Event X by May 24"      B="Event X by May 21"      (B harder: EARLIER deadline)
- A="Regime falls by June 30" B="Regime falls by May 31" (B harder: EARLIER deadline)
- A="Confirmed before 2027"  B="Confirmed by May 31"    (B harder: EARLIER/STRICTER deadline)

INVALID (do NOT include):
- Cross-timeframe: "$150k by June" and "$76k on May 18" are different time dimensions
- "$150k by June" and "$85k in May" — different time dimensions
- "peace deal by X" and "meeting by X" — probabilistic, NOT mathematical guarantee
- Any pair where implication is only likely, not certain

Return a JSON array of objects:
[
  {{
    "market_a": "<BROADER/LATER/EASIER market — the one that B guarantees>",
    "market_b": "<NARROWER/EARLIER/HARDER market — the one that implies A>",
    "reason": "<one-line: why B guarantees A>",
    "direction": "B implies A",
    "confidence": <0.0-1.0>
  }}
]

Only include pairs with confidence >= 0.95 (strict logical guarantees only). If none found, return [].
"""


def _api_call(questions: list[str], api_key: str) -> list[dict]:
    numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    prompt   = _USER_TEMPLATE.format(markets=numbered)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":       DEEPSEEK_MODEL,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
    }

    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(DEEPSEEK_API_URL, headers=headers,
                              json=payload, timeout=TIMEOUT_SEC)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if present
            if content.startswith("```"):
                parts = content.split("```")
                content = parts[1] if len(parts) > 1 else content.lstrip("`")
                if content.startswith("json"):
                    content = content[4:]
            return json.loads(content)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < MAX_RETRIES - 1:
                time.sleep(3 * (attempt + 1))
        except (json.JSONDecodeError, KeyError, IndexError):
            return []
    return []


def _normalize_direction(p: dict) -> dict:
    """
    Ensure market_a=broader(later deadline/lower threshold) and market_b=narrower.
    Detects and corrects cases where the AI swapped A and B for deadline-type markets.
    The earlier/harder deadline must be market_b.
    """
    import re
    ma, mb = p.get("market_a", ""), p.get("market_b", "")

    # Month-rank map for deadline comparison
    _MONTH_RANK = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    def extract_year(text: str) -> int | None:
        m = re.search(r'\b(202\d)\b', text)
        return int(m.group(1)) if m else None

    def extract_month(text: str) -> int | None:
        m = re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*', text, re.I)
        return _MONTH_RANK.get(m.group(1).lower()[:3]) if m else None

    # For deadline markets: market_a should have the LATER deadline.
    # If market_a has an EARLIER month than market_b → direction is backwards → swap.
    yr_a, yr_b = extract_year(ma), extract_year(mb)
    mo_a, mo_b = extract_month(ma), extract_month(mb)

    # Only auto-swap when both have the same event structure (shared base text after removing dates)
    base_a = re.sub(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s*\d{0,2},?\s*\d{4}?\b', 'X', ma, flags=re.I)
    base_b = re.sub(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s*\d{0,2},?\s*\d{4}?\b', 'X', mb, flags=re.I)
    base_a = re.sub(r'\b202\d\b', 'X', base_a)
    base_b = re.sub(r'\b202\d\b', 'X', base_b)

    if base_a.lower() == base_b.lower() and mo_a and mo_b:
        yr_a = yr_a or 2026
        yr_b = yr_b or 2026
        deadline_a = yr_a * 12 + mo_a
        deadline_b = yr_b * 12 + mo_b
        if deadline_a < deadline_b:
            # market_a has EARLIER deadline → should be market_b
            p = dict(p)
            p["market_a"], p["market_b"] = mb, ma
            logging.debug(f"DIRECTION_FIX | swapped: now A={mb[:40]} B={ma[:40]}")
    return p


def _clean_pairs(raw: list[dict]) -> list[dict]:
    """Filter junk pairs and normalize direction."""
    seen: set[tuple] = set()
    result = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        ma, mb = p.get("market_a", ""), p.get("market_b", "")
        if not ma or not mb or ma == mb:
            continue
        if float(p.get("confidence", 0)) < 0.95:
            continue
        p = _normalize_direction(p)
        key = (p.get("market_a", ""), p.get("market_b", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


def find_related_pairs(questions: list[str]) -> list[dict]:
    """
    AI-driven logical pair detection with SQLite cache (24h TTL).

    Resolution order:
    1. Fresh cache (within TTL)  → return immediately, no API call
    2. Live API / heuristic      → save to cache, return result
    3. Stale cache (TTL expired) → return last known-good result
    4. No cache at all           → return []

    Patrol never stops: empty-list API failures are NOT cached (database.py
    save_pair_cache skips empty), so step 3 always finds the last success.
    """
    from modules.database import (make_cache_key, get_cached_pairs,
                                   save_pair_cache, get_latest_cached_pairs)

    cache_key = make_cache_key(questions)

    # 1. Fresh cache hit — re-run filter in case threshold was raised since caching
    cached = get_cached_pairs(cache_key)
    if cached is not None:
        filtered = _clean_pairs(cached)
        if filtered:
            return filtered
        # All cached pairs filtered out → fall through to live call

    # 2. Live call
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    raw     = _api_call(questions, api_key) if api_key else _heuristic_fallback(questions)
    pairs   = _clean_pairs(raw)

    if pairs:
        save_pair_cache(cache_key, pairs)   # only persists non-empty results
        return pairs

    # 3. Stale fallback — API failed or returned nothing
    stale = get_latest_cached_pairs(cache_key)
    if stale is not None:
        logging.warning("AI_PAIRS | live call returned empty; using stale cache")
        return stale

    # 4. No history at all
    return []


def _heuristic_fallback(questions: list[str]) -> list[dict]:
    """
    No API key: rule-based heuristic for common threshold patterns.
    Catches patterns like '$70k' and '$80k' in the same question set.
    """
    import re
    pairs = []
    dollar_pattern = re.compile(r"\$(\d+(?:\.\d+)?)(k|m|b)?", re.IGNORECASE)

    for i, qa in enumerate(questions):
        for j, qb in enumerate(questions):
            if i >= j:
                continue
            m_a = dollar_pattern.search(qa)
            m_b = dollar_pattern.search(qb)
            if not m_a or not m_b:
                continue
            # Extract base question (remove the threshold number)
            base_a = dollar_pattern.sub("$X", qa)
            base_b = dollar_pattern.sub("$X", qb)
            if base_a.lower() != base_b.lower():
                continue

            val_a = float(m_a.group(1)) * {"k": 1e3, "m": 1e6, "b": 1e9}.get(
                (m_a.group(2) or "").lower(), 1)
            val_b = float(m_b.group(1)) * {"k": 1e3, "m": 1e6, "b": 1e9}.get(
                (m_b.group(2) or "").lower(), 1)

            if val_a < val_b:
                pairs.append({
                    "market_a":   qa, "market_b": qb,
                    "reason":    f"Exceeding ${m_b.group(0)} implies exceeding ${m_a.group(0)}",
                    "direction": "B implies A",
                    "confidence": 0.95,
                    "source":    "heuristic",
                })
    return pairs


def analyze_price_spike(question: str, change_pct: float) -> str:
    """
    Volatility-singularity analysis: called when YES price moves ≥3% in ~5 minutes.
    Asks DeepSeek for the most likely real-world cause of the rapid movement.
    """
    api_key   = os.environ.get("DEEPSEEK_API_KEY", "")
    direction = "surged" if change_pct > 0 else "dropped"
    if not api_key:
        return (f"No API key — {abs(change_pct):.1%} {direction}: "
                "likely breaking news or sudden liquidity shift.")

    prompt = (
        f'Prediction market: "{question}"\n\n'
        f"The YES price {direction} by {abs(change_pct):.1%} in approximately 5 minutes. "
        "In 2–3 sentences, speculate on the most likely real-world cause "
        "(e.g., breaking news, poll release, social media event, coordinated trading). "
        "Be concise and specific."
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model":       DEEPSEEK_MODEL,
        "temperature": 0.4,
        "max_tokens":  150,
        "messages": [
            {"role": "system",
             "content": "You are a prediction market analyst specializing in price-impact analysis."},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        r = requests.post(DEEPSEEK_API_URL, headers=headers,
                          json=payload, timeout=TIMEOUT_SEC)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Analysis unavailable: {e}"


def analyze_why_distorted(question: str) -> str:
    """
    Post-event analysis (called ~10 min after edge detection):
    ask DeepSeek why this market's price may have distorted.
    Falls back to a heuristic summary if no API key is set.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return ("No API key — likely causes: breaking news, thin order book, "
                "correlated asset shock, or algorithmic repricing.")

    prompt = (
        f'Prediction market: "{question}"\n\n'
        "This market's YES+NO sum dropped significantly below 1.0, suggesting "
        "a pricing inefficiency. In 2–3 sentences, speculate on the most likely "
        "real-world cause (e.g., breaking news, thin liquidity, correlated market "
        "shock, data feed delay). Be concise and specific."
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model":       DEEPSEEK_MODEL,
        "temperature": 0.3,
        "max_tokens":  150,
        "messages": [
            {"role": "system", "content": "You are a prediction market analyst. Answer concisely."},
            {"role": "user",   "content": prompt},
        ],
    }
    try:
        r = requests.post(DEEPSEEK_API_URL, headers=headers,
                          json=payload, timeout=TIMEOUT_SEC)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Analysis unavailable: {e}"
