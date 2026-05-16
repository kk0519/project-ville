#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  PROJECT VILE (ヴィレ)  —  Rich Dashboard v2.0                  ║
║  Professional real-time Polymarket monitoring console            ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import logging
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from rich.console   import Console
from rich.table     import Table
from rich.panel     import Panel
from rich.layout    import Layout
from rich.live      import Live
from rich.text      import Text
from rich.columns   import Columns
from rich import box

from modules.fetcher     import fetch_active_markets, fetch_market_by_id, parse_prices
from modules.detector    import (MarketData, detect_edge, calculate_ev,
                                 check_logical_inconsistency, scan_triangle_arb)
from modules.calculator  import simulate_trade, optimal_position_size
from modules.stats       import Stats, TradeRecord
from modules.database    import (init_db, save_snapshot, save_edge_event, save_trade,
                                 get_daily_ev, get_today_summary,
                                 save_anomaly_analysis, cleanup_old_snapshots)
from modules.ai_analyst  import find_related_pairs, analyze_why_distorted
from modules.notifier    import notify_edge_anomaly, notify_triangle_anomaly, channels_active

# ── Config ───────────────────────────────────────────────────────
CAPITAL_JPY      = 100_000
EDGE_THRESHOLD   = 0.02
POLL_INTERVAL    = 60
MARKET_LIMIT     = 15
LOG_FILE           = Path(__file__).parent / "vire_backtest.log"
LOG_TAIL_SIZE      = 10
LATENCY_EXPIRY_MS  = 2_000

console     = Console()
stats       = Stats()
log_ring:   deque[str] = deque(maxlen=LOG_TAIL_SIZE)
_prev_prices: dict[str, float] = {}

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s UTC | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ── Helpers ──────────────────────────────────────────────────────
def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str, level: str = "INFO"):
    entry = f"[{now_utc()}] {msg}"
    log_ring.append(entry)
    getattr(logging, level.lower())(msg)


def simulate_latency() -> float:
    return max(50.0, random.uniform(30, 200) + random.gauss(800, 600))


def schedule_post_analysis(market_id: str, question: str, edge: float):
    """Fire DeepSeek post-analysis 10 minutes after edge detection; store result in DB."""
    def _run():
        analysis = analyze_why_distorted(question)
        save_anomaly_analysis(market_id, question, edge, analysis, "deepseek-chat")
        log(f"AI分析完了 | {question[:40]} | {analysis[:80]}")
    t = threading.Timer(600, _run)
    t.daemon = True
    t.start()


def log_virtual_trade(md: MarketData, side: str, sim: dict, latency_ms: float):
    expired = latency_ms > LATENCY_EXPIRY_MS
    pnl     = 0.0 if expired else sim["expected_value"]
    trade   = TradeRecord(
        timestamp   = now_utc(),
        market      = md.question[:70],
        side        = side,
        entry_price = md.yes_price if side == "YES" else md.no_price,
        capital     = CAPITAL_JPY,
        pnl         = pnl,
        is_win      = pnl > 0,
    )
    stats.add(trade)
    status = f"EXPIRED({latency_ms:.0f}ms)" if expired else f"{'WIN' if pnl>0 else 'LOSS'}"
    log(f"TRADE | {trade.market} | {trade.side} @ {trade.entry_price:.4f} | "
        f"EV=¥{pnl:,.0f} | latency={latency_ms:.0f}ms | {status}")
    save_trade(md.market_id, md.question, side, trade.entry_price,
               CAPITAL_JPY, sim["fee"], pnl,
               sim["roi_if_win_pct"], latency_ms, expired)


# ── Rich Components ──────────────────────────────────────────────
def make_header(uptime_sec: float, last_latency_ms: float) -> Panel:
    h, m, s = int(uptime_sec//3600), int((uptime_sec%3600)//60), int(uptime_sec%60)
    pnl      = stats.cumulative_pnl
    pnl_col  = "green" if pnl >= 0 else "red"

    t = Text(justify="center")
    t.append("PROJECT VILE  ▶  Polymarket Simulation Engine\n", style="bold cyan")
    t.append(f"  総仮想損益: ", style="white")
    t.append(f"¥{pnl:+,.0f}  ", style=f"bold {pnl_col}")
    t.append(f"│  稼働時間: {h:02d}:{m:02d}:{s:02d}  ", style="white")
    t.append(f"│  APIレイテンシ: {last_latency_ms:.0f}ms  ", style="white")
    t.append(f"│  {now_utc()}", style="dim")
    return Panel(t, style="cyan", box=box.DOUBLE)


def make_market_table(markets: list[MarketData]) -> Table:
    tbl = Table(
        box=box.SIMPLE_HEAD,
        header_style="bold cyan",
        show_lines=False,
        expand=True,
    )
    tbl.add_column("#",       width=3,  justify="right")
    tbl.add_column("Market",  min_width=42, no_wrap=True)
    tbl.add_column("YES",     width=7,  justify="right")
    tbl.add_column("NO",      width=7,  justify="right")
    tbl.add_column("SUM",     width=7,  justify="right")
    tbl.add_column("EDGE",    width=8,  justify="right")
    tbl.add_column("Best",    width=5,  justify="center")
    tbl.add_column("EV%",     width=8,  justify="right")
    tbl.add_column("推奨サイズ", width=12, justify="right")

    for i, md in enumerate(markets, 1):
        is_edge = detect_edge(md, EDGE_THRESHOLD)
        ev      = calculate_ev(md.yes_price, md.no_price)
        pos     = optimal_position_size(md.yes_price, capital=CAPITAL_JPY)

        row_style = "bold yellow on dark_orange3" if is_edge else ""
        flag      = " ⚡" if is_edge else ""

        edge_color = "red bold" if md.edge < -EDGE_THRESHOLD else ("green" if md.edge > 0 else "white")
        ev_color   = "green" if ev["best_ev_pct"] > 0 else "red"

        tbl.add_row(
            str(i),
            md.question[:48] + flag,
            f"{md.yes_price:.4f}",
            f"{md.no_price:.4f}",
            Text(f"{md.total:.4f}", style="bold yellow" if is_edge else "white"),
            Text(f"{md.edge:+.4f}", style=edge_color),
            Text(ev["best_side"], style="cyan"),
            Text(f"{ev['best_ev_pct']:+.2f}%", style=ev_color),
            pos["recommended_size_jpy"],
            style=row_style,
        )

    return tbl


def make_stats_panel() -> Panel:
    today = get_today_summary()
    t = Text()
    t.append("MQL5統計  │  ", style="bold cyan")
    t.append(stats.summary_line(), style="white")
    if today.get("total"):
        exp = today.get("expired_count", 0)
        t.append(f"  │  本日: {today['total']}件 失効={exp}件", style="dim")
    return Panel(t, title="[cyan]Real-time Stats[/cyan]", box=box.ROUNDED)


def make_ev_chart() -> Panel:
    """Weekly EV bar chart from SQLite data."""
    rows = get_daily_ev(days=7)
    t    = Text()
    if not rows:
        t.append("データ蓄積中...", style="dim")
        return Panel(t, title="[cyan]週次EV推移 (SQLite)[/cyan]",
                     box=box.ROUNDED, height=10)

    max_abs = max((abs(r["ev_sum"] or 0) for r in rows), default=1) or 1
    BAR_W   = 20
    for r in rows:
        day  = r["day"]
        val  = r["ev_sum"] or 0
        bars = int(abs(val) / max_abs * BAR_W)
        bar  = "█" * bars
        color = "green" if val >= 0 else "red"
        t.append(f"  {day}  ", style="white")
        t.append(f"{bar:<{BAR_W}}", style=color)
        t.append(f"  ¥{val:+,.0f}  ({r['trades']}件)\n", style=color)

    return Panel(t, title="[cyan]週次 仮想EV推移 (SQLite)[/cyan]",
                 box=box.ROUNDED, height=min(10, len(rows) + 3))


def make_log_panel() -> Panel:
    t = Text()
    if not log_ring:
        t.append("ログなし", style="dim")
    else:
        for line in list(log_ring):
            if "EXPIRED" in line:
                color = "red"
            elif "ALERT" in line:
                color = "yellow"
            elif "TRADE" in line:
                color = "green"
            else:
                color = "dim"
            t.append(line + "\n", style=color)
    return Panel(t, title=f"[cyan]Recent Log (last {LOG_TAIL_SIZE})[/cyan]",
                 box=box.ROUNDED, height=14)


# ── Core cycle ───────────────────────────────────────────────────
def run_cycle(market_ids: list[str]) -> tuple[list[MarketData], float]:
    t0 = time.time()
    markets: list[MarketData] = []

    for mid in market_ids:
        raw = fetch_market_by_id(mid)
        if not raw:
            log(f"WARN | {mid} 取得失敗", "warning")
            continue
        yes_p, no_p = parse_prices(raw)
        if yes_p is None:
            continue
        markets.append(MarketData(raw.get("question", mid), mid, yes_p, no_p))

    latency_ms = (time.time() - t0) * 1000

    for md in markets:
        save_snapshot(md.market_id, md.question, md.yes_price, md.no_price)

        prev_yes = _prev_prices.get(md.market_id, md.yes_price)
        pch = (md.yes_price - prev_yes) / max(prev_yes, 0.001)

        if detect_edge(md, EDGE_THRESHOLD):
            latency_ms = simulate_latency()
            ev  = calculate_ev(md.yes_price, md.no_price)
            sim = simulate_trade(md.yes_price, md.no_price, CAPITAL_JPY, ev["best_side"])
            log_virtual_trade(md, ev["best_side"], sim, latency_ms)
            save_edge_event(md.market_id, md.question,
                            md.yes_price, md.no_price,
                            sim["expected_value"], ev["best_side"],
                            latency_ms, latency_ms > LATENCY_EXPIRY_MS,
                            price_change_rate=pch)
            log(f"ALERT | {md.question[:50]}  SUM={md.total:.4f}  ΔP={pch:+.2%}  lat={latency_ms:.0f}ms", "warning")
            notify_edge_anomaly(md.question, md.edge, sim["expected_value"],
                                ev["best_side"], pch)
            schedule_post_analysis(md.market_id, md.question, md.edge)

    for md in markets:
        _prev_prices[md.market_id] = md.yes_price

    for i in range(len(markets) - 1):
        arb = check_logical_inconsistency(markets[i], markets[i + 1])
        if arb:
            log(f"ARB | {arb['strategy'][:80]}", "warning")
            logging.info(f"ARB_DETAIL | {json.dumps(arb, ensure_ascii=False)}")

    triangles = scan_triangle_arb(markets, min_compound=0.005)
    for tri in triangles[:5]:
        log(f"TRIANGLE | {tri['strategy'][:80]}", "warning")
        notify_triangle_anomaly(tri["chain"], tri["prices"], tri["compound_profit"])

    return markets, latency_ms


# ── Main ─────────────────────────────────────────────────────────
def main():
    console.print("[cyan]PROJECT VILE[/cyan] 起動中 — DB初期化・市場リスト取得中...")
    init_db()

    raw = fetch_active_markets(limit=MARKET_LIMIT)
    if not raw:
        console.print("[red]市場取得失敗。終了。[/red]")
        sys.exit(1)

    market_ids = [m.get("conditionId") or m.get("id", "") for m in raw]
    market_ids = [mid for mid in market_ids if mid][:MARKET_LIMIT]

    questions = [m.get("question", "") for m in raw if m.get("question")]
    ai_pairs  = find_related_pairs(questions)
    channels  = channels_active()
    ch_str    = "/".join(channels) if channels else "なし（DISCORD_WEBHOOK_URL等 未設定）"
    console.print(f"[green]監視市場: {len(market_ids)}件  AI依存ペア: {len(ai_pairs)}件  "
                  f"通知チャネル: {ch_str}[/green]\n")

    start_time   = time.time()
    last_latency = 0.0
    cycle        = 0
    markets      = []

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            cycle += 1
            uptime = time.time() - start_time

            try:
                markets, last_latency = run_cycle(market_ids)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"CYCLE_ERROR | cycle={cycle} | {e}", "error")

            layout = Layout()
            layout.split_column(
                Layout(make_header(uptime, last_latency), size=4),
                Layout(make_market_table(markets)),
                Layout(make_stats_panel(), size=3),
                Layout(make_ev_chart(), size=10),
                Layout(make_log_panel(), size=14),
            )
            live.update(layout)
            time.sleep(POLL_INTERVAL)
            if cycle % 1440 == 0:  # daily maintenance (60s × 1440 ≈ 24h)
                deleted = cleanup_old_snapshots(7)
                log(f"DB CLEANUP | 古いスナップショット {deleted}件を集約・削除")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print(f"\n[cyan]VILE 終了。最終統計: {stats.summary_line()}[/cyan]")
        sys.exit(0)
