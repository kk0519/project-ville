"""fetcher.py — Polymarket API data retrieval with auto-retry"""
import json
import time
import requests

GAMMA_API = "https://gamma-api.polymarket.com"
MAX_RETRIES = 2
RETRY_BASE  = 1  # seconds


def _get(url: str, params: dict = None, retries: int = MAX_RETRIES) -> dict | list:
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=8)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            wait = RETRY_BASE * (attempt + 1)
            if attempt < retries - 1:
                time.sleep(wait)
            else:
                raise RuntimeError(f"API unreachable after {retries} attempts: {e}") from e
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"HTTP error {e.response.status_code}: {url}") from e


def fetch_active_markets(limit: int = 20) -> list[dict]:
    try:
        data = _get(f"{GAMMA_API}/markets", params={
            "active": "true", "closed": "false",
            "limit": limit, "order": "volume24hr", "ascending": "false",
        })
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_market_by_id(market_id: str) -> dict | None:
    try:
        return _get(f"{GAMMA_API}/markets/{market_id}")
    except Exception:
        return None


def parse_prices(market: dict) -> tuple[float | None, float | None]:
    raw = market.get("outcomePrices")
    if not raw:
        return None, None
    prices = json.loads(raw) if isinstance(raw, str) else raw
    try:
        return float(prices[0]), float(prices[1])
    except (IndexError, ValueError, TypeError):
        return None, None
