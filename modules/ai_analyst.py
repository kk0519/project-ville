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

_USER_TEMPLATE = """Analyze these prediction market questions for logical dependencies:

{markets}

Return a JSON array of objects:
[
  {{
    "market_a": "<question of the broader/lower-threshold market>",
    "market_b": "<question of the narrower/higher-threshold market>",
    "reason": "<one-line explanation of the logical dependency>",
    "direction": "B implies A",
    "confidence": <0.0-1.0>
  }}
]

Only include pairs with confidence >= 0.6. If none found, return [].
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
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            return json.loads(content)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt < MAX_RETRIES - 1:
                time.sleep(3 * (attempt + 1))
        except (json.JSONDecodeError, KeyError, IndexError):
            return []
    return []


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

    # 1. Fresh cache hit
    cached = get_cached_pairs(cache_key)
    if cached is not None:
        return cached

    # 2. Live call
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    pairs   = _api_call(questions, api_key) if api_key else _heuristic_fallback(questions)

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
