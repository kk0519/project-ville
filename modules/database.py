"""database.py — SQLite persistence: market snapshots, edge events, trades, AI cache, trends"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

GENRE_KEYWORDS: dict[str, list[str]] = {
    "crypto":    ["bitcoin", "btc", "eth", "ethereum", "crypto", "solana", "doge", "xrp"],
    "politics":  ["president", "election", "trump", "senate", "congress", "vote", "democrat",
                  "republican", "prime minister", "chancellor"],
    "sports":    ["game", "match", "championship", "league", "win", "cup", "nba", "nfl",
                  "mlb", "lol", "esport"],
    "finance":   ["fed", "fomc", "rate", "gdp", "inflation", "dow", "nasdaq", "sp500",
                  "recession", "cpi"],
    "other":     [],
}

DB_PATH = Path(__file__).parent.parent / "ville_data.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # WAL: allows concurrent reads (ville_web.py) while writes are in progress.
    # NORMAL synchronous: safe after crash (WAL guarantees durability without fsync).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def _cursor():
    conn = _connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _cursor() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            market_id   TEXT    NOT NULL,
            question    TEXT    NOT NULL,
            yes_price   REAL,
            no_price    REAL,
            total       REAL,
            edge        REAL
        );
        CREATE TABLE IF NOT EXISTS edge_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            market_id   TEXT    NOT NULL,
            question    TEXT    NOT NULL,
            yes_price   REAL,
            no_price    REAL,
            total       REAL,
            edge        REAL,
            ev_jpy      REAL,
            side        TEXT,
            latency_ms  REAL,
            expired     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS virtual_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            market_id   TEXT    NOT NULL,
            question    TEXT    NOT NULL,
            side        TEXT,
            entry_price REAL,
            capital     REAL,
            fee         REAL,
            ev_jpy      REAL,
            roi_win_pct REAL,
            latency_ms  REAL,
            expired     INTEGER DEFAULT 0,
            is_win      INTEGER
        );
        CREATE TABLE IF NOT EXISTS ai_pair_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key    TEXT    UNIQUE NOT NULL,
            pairs_json   TEXT    NOT NULL,
            created_at   TEXT    NOT NULL,
            expires_at   TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snap_ts    ON market_snapshots(ts);
        CREATE INDEX IF NOT EXISTS idx_trade_ts   ON virtual_trades(ts);
        CREATE INDEX IF NOT EXISTS idx_edge_ts    ON edge_events(ts);
        CREATE INDEX IF NOT EXISTS idx_cache_key  ON ai_pair_cache(cache_key);
        CREATE TABLE IF NOT EXISTS anomaly_analysis (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            market_id TEXT NOT NULL,
            question  TEXT NOT NULL,
            edge      REAL,
            analysis  TEXT,
            model     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_anomaly_ts ON anomaly_analysis(ts);
        """)
        for sql in (
            "ALTER TABLE edge_events ADD COLUMN price_change_rate REAL DEFAULT 0",
            "ALTER TABLE edge_events ADD COLUMN context_json TEXT DEFAULT ''",
        ):
            try:
                c.execute(sql)
            except Exception:
                pass


def save_snapshot(market_id: str, question: str,
                  yes_p: float, no_p: float):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as c:
        c.execute("""INSERT INTO market_snapshots
                     (ts,market_id,question,yes_price,no_price,total,edge)
                     VALUES (?,?,?,?,?,?,?)""",
                  (ts, market_id, question[:120], yes_p, no_p,
                   yes_p + no_p, (yes_p + no_p) - 1.0))


def save_snapshots_bulk(rows: list[tuple]):
    """Batch-insert all market snapshots in a single transaction.

    Replaces N individual save_snapshot() calls with one executemany().
    At 50 markets/cycle this eliminates 49 redundant connection open/close pairs.

    rows: list of (ts, market_id, question, yes_p, no_p, total, edge)
    """
    if not rows:
        return
    with _cursor() as c:
        c.executemany("""INSERT INTO market_snapshots
                         (ts, market_id, question, yes_price, no_price, total, edge)
                         VALUES (?,?,?,?,?,?,?)""", rows)


def save_edge_event(market_id: str, question: str,
                    yes_p: float, no_p: float,
                    ev_jpy: float, side: str,
                    latency_ms: float, expired: bool,
                    price_change_rate: float = 0.0,
                    context_json: str = ""):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as c:
        c.execute("""INSERT INTO edge_events
                     (ts,market_id,question,yes_price,no_price,total,edge,
                      ev_jpy,side,latency_ms,expired,price_change_rate,context_json)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (ts, market_id, question[:120], yes_p, no_p,
                   yes_p + no_p, (yes_p + no_p) - 1.0,
                   ev_jpy, side, latency_ms, int(expired),
                   price_change_rate, context_json))


def save_trade(market_id: str, question: str, side: str,
               entry_price: float, capital: float, fee: float,
               ev_jpy: float, roi_win_pct: float,
               latency_ms: float, expired: bool):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as c:
        c.execute("""INSERT INTO virtual_trades
                     (ts,market_id,question,side,entry_price,capital,fee,
                      ev_jpy,roi_win_pct,latency_ms,expired,is_win)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (ts, market_id, question[:120], side, entry_price,
                   capital, fee, ev_jpy, roi_win_pct,
                   latency_ms, int(expired), int(ev_jpy > 0)))


def get_daily_ev(days: int = 7) -> list[dict]:
    with _cursor() as c:
        rows = c.execute("""
            SELECT DATE(ts) AS day,
                   SUM(CASE WHEN expired=0 THEN ev_jpy ELSE 0 END) AS ev_sum,
                   COUNT(*) AS trades
            FROM virtual_trades
            WHERE ts >= datetime('now', ?)
            GROUP BY DATE(ts)
            ORDER BY day
        """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def get_today_summary() -> dict:
    with _cursor() as c:
        r = c.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN is_win=1 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN expired=0 THEN ev_jpy ELSE 0 END) AS ev_sum,
                   SUM(CASE WHEN expired=1 THEN 1 ELSE 0 END) AS expired_count
            FROM virtual_trades
            WHERE DATE(ts) = DATE('now')
        """).fetchone()
    return dict(r) if r else {}


def get_edge_count_today() -> int:
    with _cursor() as c:
        r = c.execute("SELECT COUNT(*) FROM edge_events WHERE DATE(ts)=DATE('now')").fetchone()
    return r[0] if r else 0


# ── AI pair cache ─────────────────────────────────────────────────
def make_cache_key(questions: list[str]) -> str:
    payload = json.dumps(sorted(questions), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def get_cached_pairs(cache_key: str) -> list[dict] | None:
    with _cursor() as c:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = c.execute(
            "SELECT pairs_json FROM ai_pair_cache WHERE cache_key=? AND expires_at > ?",
            (cache_key, now)
        ).fetchone()
    return json.loads(row["pairs_json"]) if row else None


def save_pair_cache(cache_key: str, pairs: list[dict], ttl_hours: int = 24):
    # Never persist empty results — empty could mean API failure, not "no pairs found".
    # Stale cache is better than a cached empty list.
    if not pairs:
        return
    from datetime import timedelta
    now     = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as c:
        c.execute("""INSERT OR REPLACE INTO ai_pair_cache
                     (cache_key, pairs_json, created_at, expires_at)
                     VALUES (?,?,?,?)""",
                  (cache_key, json.dumps(pairs, ensure_ascii=False), now_str, expires))


def get_latest_cached_pairs(cache_key: str) -> list[dict] | None:
    """Stale-cache fallback: return most recent entry regardless of TTL.

    Called when a live API call fails so the patrol never stops with empty pairs.
    Returns None only if no entry has ever been saved for this cache_key.
    """
    with _cursor() as c:
        row = c.execute(
            "SELECT pairs_json FROM ai_pair_cache "
            "WHERE cache_key=? ORDER BY created_at DESC LIMIT 1",
            (cache_key,)
        ).fetchone()
    return json.loads(row["pairs_json"]) if row else None


# ── Trend analysis ────────────────────────────────────────────────
def analyze_edge_by_hour() -> list[dict]:
    """Average edge and count by hour of day (UTC) from market_snapshots."""
    with _cursor() as c:
        rows = c.execute("""
            SELECT CAST(strftime('%H', ts) AS INTEGER) AS hour,
                   AVG(edge)   AS avg_edge,
                   MIN(edge)   AS min_edge,
                   COUNT(*)    AS snap_count
            FROM market_snapshots
            GROUP BY strftime('%H', ts)
            ORDER BY hour
        """).fetchall()
    return [dict(r) for r in rows]


def analyze_edge_by_weekday() -> list[dict]:
    """Average edge by day of week (0=Sun … 6=Sat) from market_snapshots."""
    DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    with _cursor() as c:
        rows = c.execute("""
            SELECT CAST(strftime('%w', ts) AS INTEGER) AS wday,
                   AVG(edge)  AS avg_edge,
                   COUNT(*)   AS snap_count
            FROM market_snapshots
            GROUP BY strftime('%w', ts)
            ORDER BY wday
        """).fetchall()
    return [{**dict(r), "day_name": DAY_NAMES[r["wday"]]} for r in rows]


def analyze_by_genre() -> list[dict]:
    """EV statistics grouped by market genre (keyword-based classification)."""
    with _cursor() as c:
        all_snaps = c.execute(
            "SELECT question, edge FROM market_snapshots"
        ).fetchall()

    counts: dict[str, list[float]] = {g: [] for g in GENRE_KEYWORDS}
    for row in all_snaps:
        q   = row["question"].lower()
        matched = False
        for genre, kws in GENRE_KEYWORDS.items():
            if genre == "other":
                continue
            if any(kw in q for kw in kws):
                counts[genre].append(row["edge"])
                matched = True
                break
        if not matched:
            counts["other"].append(row["edge"])

    results = []
    for genre, edges in counts.items():
        if not edges:
            continue
        results.append({
            "genre":    genre,
            "count":    len(edges),
            "avg_edge": sum(edges) / len(edges),
            "min_edge": min(edges),
            "max_edge": max(edges),
        })
    return sorted(results, key=lambda x: x["avg_edge"])


def get_trend_report() -> dict:
    """Combined trend analysis report."""
    return {
        "by_hour":    analyze_edge_by_hour(),
        "by_weekday": analyze_edge_by_weekday(),
        "by_genre":   analyze_by_genre(),
        "snapshot_total": _single_int("SELECT COUNT(*) FROM market_snapshots"),
        "trade_total":    _single_int("SELECT COUNT(*) FROM virtual_trades"),
    }


def _single_int(sql: str) -> int:
    with _cursor() as c:
        r = c.execute(sql).fetchone()
    return r[0] if r else 0


def load_all_snapshots() -> list[dict]:
    """Return all market snapshots for backtesting."""
    with _cursor() as c:
        rows = c.execute(
            "SELECT * FROM market_snapshots ORDER BY ts"
        ).fetchall()
    return [dict(r) for r in rows]


def save_anomaly_analysis(market_id: str, question: str,
                          edge: float, analysis: str, model: str):
    """Store AI post-event distortion analysis."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as c:
        c.execute("""INSERT INTO anomaly_analysis
                     (ts, market_id, question, edge, analysis, model)
                     VALUES (?,?,?,?,?,?)""",
                  (ts, market_id, question[:120], edge, analysis, model))


def cleanup_old_snapshots(days_to_keep: int = 7) -> int:
    """
    Aggregate raw snapshots older than days_to_keep into hourly averages,
    then delete the raw rows. Returns count of deleted rows.
    """
    cutoff = f"-{days_to_keep} days"
    with _cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_hourly_agg (
                hour_bucket TEXT NOT NULL,
                market_id   TEXT NOT NULL,
                question    TEXT NOT NULL,
                avg_yes     REAL,
                avg_no      REAL,
                avg_edge    REAL,
                sample_n    INTEGER,
                PRIMARY KEY (hour_bucket, market_id)
            )
        """)
        c.execute("""
            INSERT OR REPLACE INTO snapshot_hourly_agg
                (hour_bucket, market_id, question, avg_yes, avg_no, avg_edge, sample_n)
            SELECT
                strftime('%Y-%m-%d %H:00', ts),
                market_id, question,
                AVG(yes_price), AVG(no_price), AVG(edge), COUNT(*)
            FROM market_snapshots
            WHERE ts < datetime('now', ?)
            GROUP BY strftime('%Y-%m-%d %H:00', ts), market_id
        """, (cutoff,))
        deleted = c.execute(
            "DELETE FROM market_snapshots WHERE ts < datetime('now', ?)",
            (cutoff,)
        ).rowcount
    return deleted


def get_recent_anomaly_analyses(limit: int = 5) -> list[dict]:
    """Return most recent AI post-event analyses."""
    with _cursor() as c:
        rows = c.execute(
            "SELECT * FROM anomaly_analysis ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_total_record_count() -> int:
    """Return combined snapshot + trade count for heartbeat logging."""
    return (_single_int("SELECT COUNT(*) FROM market_snapshots") +
            _single_int("SELECT COUNT(*) FROM virtual_trades"))
