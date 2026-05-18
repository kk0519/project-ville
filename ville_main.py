#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  PROJECT VILLE (ヴィレ)  —  Polymarket Simulation Engine v3.1    ║
║  Data Fetch → Edge Detection → Latency Sim → SQLite → Stats     ║
║  Heartbeat ・ Volatility Singularity ・ Self-Maintenance         ║
║  Notification: Discord / LINE Notify / SMTP                      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import gc
import json
import logging
import random
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from modules.fetcher      import fetch_active_markets, parse_prices
from modules.detector     import (MarketData, detect_edge, calculate_ev,
                                   calculate_effective_ev,
                                   check_logical_inconsistency, scan_triangle_arb)
from modules.calculator   import (simulate_trade, optimal_position_size,
                                   distribute_capital, distribution_summary)
from modules.orderbook    import (fetch_depth, parse_clob_token_id,
                                   depth_summary, DepthSnapshot)
from modules.stats        import Stats, TradeRecord
from modules.database     import (init_db, save_snapshots_bulk, save_edge_event,
                                  save_trade, save_anomaly_analysis,
                                  cleanup_old_snapshots, cleanup_expired_pair_cache,
                                  get_total_record_count)
from modules.ai_analyst   import find_related_pairs, analyze_price_spike
from modules.notifier     import (notify_edge_anomaly, notify_triangle_anomaly,
                                   notify_volatility_singularity, notify_startup,
                                   channels_active)

# ── ANSI ─────────────────────────────────────────────────────────
RED    = "\033[91m";  GREEN  = "\033[92m"
YELLOW = "\033[93m";  CYAN   = "\033[96m"
BOLD   = "\033[1m";   RESET  = "\033[0m"

# ── Config ───────────────────────────────────────────────────────
CAPITAL_JPY        = 100_000
EDGE_THRESHOLD     = 0.005   # 0.5%: Polymarket sums cluster at 1.000; 2% never fires
POLL_INTERVAL      = 15        # seconds between cycles (target: ~15s total cycle time)
MARKET_LIMIT       = 100       # markets per cycle (100 captures BTC threshold clusters)
LATENCY_EXPIRY_MS  = 2_000
LOG_FILE           = Path(__file__).parent / "ville_backtest.log"

VOLATILITY_THRESHOLD        = 0.03   # 3% price change triggers singularity
VOLATILITY_WINDOW_CYCLES    = 20     # ~5 min at 15s/cycle (was 5 @ 60s)
HEARTBEAT_INTERVAL_CYCLES   = 240    # ~1 hour at 15s/cycle (was 60 @ 60s)
MAINTENANCE_INTERVAL_CYCLES = 5760   # ~24 hours at 15s/cycle (was 1440 @ 60s)
AI_REFRESH_INTERVAL_CYCLES  = 5760   # refresh AI pairs every ~24h (market structure rarely changes faster)
VERBOSE_MARKETS             = False  # True = print all 50 markets; False = edges only
PHASE13_CAPITAL_JPY         = 10_000_000   # 1,000万円 scale capital model
PHASE13_DEPTH_ENABLED       = True         # fetch order book depth for edge markets
PHASE13_NUM_ACCOUNTS        = 3            # accounts to distribute across
WEB_SYNC_INTERVAL_SEC       = 600          # regenerate GitHub Pages every 10 minutes

# ── Logging setup ────────────────────────────────────────────────
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s UTC | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

stats          = Stats()
_price_history: dict[str, deque] = defaultdict(
    lambda: deque(maxlen=VOLATILITY_WINDOW_CYCLES)
)
_latency_ring: deque[float] = deque(maxlen=HEARTBEAT_INTERVAL_CYCLES)
_prev_prices:  dict[str, float] = {}

# AI pairs — updated in background thread; main loop reads without blocking
_ai_pairs:      list[dict]       = []
_ai_pairs_lock: threading.Lock  = threading.Lock()
# Questions snapshot from the last completed cycle — reused by the 24h AI refresh
# trigger so no extra fetch_active_markets() call is needed.
_last_questions: list[str]       = []


# ── Helpers ──────────────────────────────────────────────────────
def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def alert(msg: str):
    print(f"{BOLD}{RED}  ⚡ チャンス到来！ {msg}{RESET}")
    logging.info(f"ALERT | {msg}")


def simulate_latency() -> float:
    return max(50.0, random.uniform(30, 200) + random.gauss(800, 600))


def log_virtual_trade(md: MarketData, side: str, sim: dict, latency_ms: float) -> bool:
    expired = latency_ms > LATENCY_EXPIRY_MS
    pnl     = 0.0 if expired else sim["expected_value"]
    trade = TradeRecord(
        timestamp   = now_utc(),
        market      = md.question[:80],
        side        = side,
        entry_price = md.yes_price if side == "YES" else md.no_price,
        capital     = CAPITAL_JPY,
        pnl         = pnl,
        is_win      = pnl > 0,
    )
    stats.add(trade)
    status = f"EXPIRED({latency_ms:.0f}ms>2000ms)" if expired else f"{'WIN' if pnl>0 else 'LOSS'}"
    logging.info(
        f"TRADE | market={trade.market} | side={trade.side} "
        f"@ {trade.entry_price:.4f} | capital=¥{trade.capital:,} "
        f"| fee=¥{sim['fee']:,.0f} | EV=¥{pnl:,.0f} "
        f"| latency={latency_ms:.0f}ms | {status}"
    )
    save_trade(
        market_id=md.market_id, question=md.question, side=side,
        entry_price=trade.entry_price, capital=CAPITAL_JPY,
        fee=sim["fee"], ev_jpy=pnl,
        roi_win_pct=sim["roi_if_win_pct"],
        latency_ms=latency_ms, expired=expired,
    )
    return expired


# ── Heartbeat ────────────────────────────────────────────────────
def write_heartbeat(market_count: int):
    avg_lat = sum(_latency_ring) / len(_latency_ring) if _latency_ring else 0.0
    total   = get_total_record_count()
    msg = (f"[HEARTBEAT] Active Markets: {market_count}, "
           f"Avg Latency: {avg_lat:.0f}ms, DB Records: {total}, "
           f"Stats Trades: {stats.total}")
    logging.info(msg)
    print(f"  {CYAN}{msg}{RESET}")
    # Cap in-memory trade list and release unreachable objects
    stats.trim(max_records=10_000)
    gc.collect()


# ── Volatility singularity detection ─────────────────────────────
def _fire_singularity_analysis(md: MarketData, change: float):
    """Background: DeepSeek analysis → DB + notification."""
    # Notification first (fast path)
    notify_volatility_singularity(md.question, change, md.yes_price)
    # AI analysis (slower path, may take seconds)
    analysis = analyze_price_spike(md.question, change)
    save_anomaly_analysis(md.market_id, md.question,
                          md.edge, analysis, "deepseek-chat:volatility")
    logging.info(f"SINGULARITY_ANALYSIS | {md.question[:50]} | {analysis[:100]}")


def _refresh_ai_pairs_bg(questions: list[str]):
    """Background: call DeepSeek (or heuristic) and update _ai_pairs atomically."""
    global _ai_pairs
    try:
        pairs = find_related_pairs(questions)
        with _ai_pairs_lock:
            _ai_pairs = pairs
        logging.info(f"AI_PAIRS_REFRESH | {len(pairs)} pairs updated")
    except Exception as e:
        logging.warning(f"AI_PAIRS_REFRESH_FAIL | {e}")


def _notify_bg(fn, *args, **kwargs):
    """Fire-and-forget wrapper: run notification in daemon thread."""
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


def _web_sync_worker():
    """
    Background daemon: regenerate docs/index.html from live DB every
    WEB_SYNC_INTERVAL_SEC seconds, then git commit+push only if changed.
    Runs forever — no AI calls, no heavy computation.
    """
    import subprocess
    from jinja2 import Environment

    # Lazy import to avoid circular dep at module level
    from ville_web import MILESTONES, CURRENT_PHASE, CHANGELOG, HTML, get_dashboard_data

    docs_path = Path(__file__).parent / "docs" / "index.html"

    while True:
        time.sleep(WEB_SYNC_INTERVAL_SEC)
        try:
            data = get_dashboard_data()
            env  = Environment(autoescape=True)
            env.globals["max"] = max
            html = env.from_string(HTML).render(
                milestones=MILESTONES, data=data,
                current_phase=CURRENT_PHASE, changelog=CHANGELOG
            )
            new_bytes = html.encode("utf-8")

            # Only write + push if content actually changed
            if docs_path.exists() and docs_path.read_bytes() == new_bytes:
                continue

            docs_path.write_bytes(new_bytes)

            repo = str(Path(__file__).parent)
            subprocess.run(
                ["git", "-C", repo, "add", "docs/index.html"],
                capture_output=True, timeout=15
            )
            result = subprocess.run(
                ["git", "-C", repo, "commit", "-m",
                 f"auto: web sync {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"],
                capture_output=True, timeout=15
            )
            if result.returncode == 0:
                subprocess.run(
                    ["git", "-C", repo, "push", "origin", "main"],
                    capture_output=True, timeout=30
                )
                logging.info("WEB_SYNC | pushed to GitHub Pages")
        except Exception as e:
            logging.warning(f"WEB_SYNC_FAIL | {e}")


def check_volatility_singularity(markets: list[MarketData], cycle: int):
    """
    Compare current YES price vs 5 cycles ago. If change ≥ 3%, fire
    volatility singularity: log + background notification + AI analysis.
    """
    for md in markets:
        hist = _price_history[md.market_id]
        if len(hist) == VOLATILITY_WINDOW_CYCLES:
            oldest = hist[0]
            if oldest > 0:
                change = (md.yes_price - oldest) / oldest
                if abs(change) >= VOLATILITY_THRESHOLD:
                    direction = "急騰" if change > 0 else "急落"
                    logging.warning(
                        f"VOLATILITY_SINGULARITY | {md.question[:50]} | "
                        f"ΔP={change:+.2%} ({direction}) | "
                        f"YES={md.yes_price:.4f} | cycle={cycle}"
                    )
                    print(f"\n  {BOLD}{YELLOW}⚠ ボラティリティ特異点検知{RESET}  "
                          f"{md.question[:55]}")
                    print(f"     価格変化: {change:+.2%} ({direction})  "
                          f"YES現在値: {md.yes_price:.4f}\n")
                    threading.Thread(
                        target=_fire_singularity_analysis,
                        args=(md, change), daemon=True
                    ).start()
        hist.append(md.yes_price)


# ── Self-maintenance ──────────────────────────────────────────────
def rotate_log_file() -> str:
    stamp   = datetime.now(timezone.utc).strftime("%Y%m%d")
    archive = LOG_FILE.parent / f"ville_backtest_{stamp}.log"
    root = logging.getLogger()
    for h in root.handlers[:]:
        if isinstance(h, logging.FileHandler):
            h.close()
            root.removeHandler(h)
    if LOG_FILE.exists():
        LOG_FILE.rename(archive)
    fh = logging.FileHandler(str(LOG_FILE))
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s UTC | %(message)s",
                                       datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    logging.info(f"LOG_ROTATE | archived → {archive.name}")
    return archive.name


def do_daily_maintenance():
    print(f"\n  {CYAN}[MAINTENANCE] 日次セルフメンテナンス開始...{RESET}")
    archived = rotate_log_file()
    print(f"  ✓ ログローテーション完了: {archived}")
    gc.collect()
    print(f"  ✓ GC実行完了（メモリ解放）")
    deleted = cleanup_old_snapshots(days_to_keep=7)
    print(f"  ✓ DBクリーンアップ完了: {deleted}件の古いスナップショットを集約・削除")
    expired_cache = cleanup_expired_pair_cache()
    if expired_cache:
        print(f"  ✓ AIキャッシュ期限切れ削除: {expired_cache}件")
    logging.info(
        f"MAINTENANCE | log_archived={archived} | gc=done | "
        f"snapshots_deleted={deleted} | cache_expired_deleted={expired_cache}"
    )
    print(f"  {CYAN}[MAINTENANCE] 完了{RESET}\n")


# ── Display ──────────────────────────────────────────────────────
def print_notification_guide():
    # powershell.exe (Win PS5) is broken on this machine — use pwsh.exe / cmd.exe / WSL
    print(f"\n{YELLOW}{'─'*78}")
    print("  [通知設定ガイド]  環境変数をセットすると通知が有効になります")
    print(f"{'─'*78}{RESET}")

    print("  ★ ① Discord Webhook【推奨・最も簡単】")
    print("    手順: Discordサーバー → チャンネル編集 → 連携サービス → Webhookを作成 → URLをコピー")
    print()
    print("    # WSL / bash（今すぐ有効 / 永続化は ~/.bashrc に追記）:")
    print("    export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/ID/TOKEN'")
    print()
    print("    # Windows cmd.exe（※powershell.exeは破損中・使用不可）:")
    print("    set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/ID/TOKEN")
    print()
    print("    # Windows PowerShell 7（pwsh.exeのみ正常）:")
    print("    $env:DISCORD_WEBHOOK_URL = 'https://discord.com/api/webhooks/ID/TOKEN'")
    print()

    print("  ② LINE Messaging API【LINE Notifyは2025年3月終了・移行必須】")
    print("    手順: https://developers.line.biz/ → プロバイダー作成 → Messaging APIチャンネル作成")
    print("          → Channel Access Token 発行 / LINE User ID を「チャンネル基本設定」で確認")
    print()
    print("    # WSL / bash:")
    print("    export LINE_CHANNEL_ACCESS_TOKEN='your-channel-access-token'")
    print("    export LINE_USER_ID='U1234567890abcdef...'   # 自分のLINE User ID")
    print()

    print("  ③ Gmail（SMTP）")
    print("    # Google アカウント → セキュリティ → アプリパスワード で専用パスワード発行")
    print("    export VILE_ALERT_EMAIL='you@gmail.com'")
    print("    export VILE_SMTP_FROM='bot-account@gmail.com'")
    print("    export VILE_SMTP_PASS='xxxx-xxxx-xxxx-xxxx'  # 16桁アプリパスワード")
    print()
    print(f"  {YELLOW}※ powershell.exe(Win PS5)は破損中 → WSL bash / cmd.exe / pwsh.exe を使用{RESET}")
    print(f"{YELLOW}{'─'*78}{RESET}\n")


def print_banner(cycle: int):
    print(f"\n{CYAN}{'═'*78}")
    print(f"  PROJECT VILLE v3.1  ▶  Cycle #{cycle:03d}  [{now_utc()}]")
    print(f"  仮想資金: ¥{CAPITAL_JPY:,}  │  エッジ閾値: SUM≤{1-EDGE_THRESHOLD:.2f}  │  "
          f"ボラ検知: {VOLATILITY_THRESHOLD:.0%}/{VOLATILITY_WINDOW_CYCLES}min  │  "
          f"HB: {HEARTBEAT_INTERVAL_CYCLES}cyc毎")
    print(f"{'═'*78}{RESET}\n")


def print_market(idx: int, md: MarketData, ev: dict, pos: dict):
    is_edge = detect_edge(md, EDGE_THRESHOLD)
    color   = f"{BOLD}{YELLOW}" if is_edge else ""
    flag    = f"  {BOLD}{RED}◀ EDGE DETECTED{RESET}" if is_edge else ""
    print(f"{color}  {idx:>2}. {md.question[:60]:<60}{RESET}{flag}")
    print(f"      YES={md.yes_price:.4f}  NO={md.no_price:.4f}  "
          f"SUM={BOLD}{md.total:.4f}{RESET}  "
          f"EDGE={md.edge:+.4f}  │  "
          f"最良={ev['best_side']}  EV={ev['best_ev_pct']:+.2f}%  "
          f"推奨={pos['recommended_size_jpy']}")


def print_stats_panel():
    print(f"\n{CYAN}{'─'*78}")
    print(f"  MQL5スタイル統計サマリー")
    print(f"  {stats.summary_line()}")
    if stats.last_n(3):
        print(f"  直近取引:")
        for t in stats.last_n(3):
            pnl_color = GREEN if t.pnl > 0 else RED
            print(f"    [{t.timestamp}] {t.market[:45]:<45} "
                  f"{t.side} {pnl_color}EV=¥{t.pnl:,.0f}{RESET}")
    print(f"{'─'*78}{RESET}")


# ── Main loop ────────────────────────────────────────────────────
def run_cycle(cycle: int) -> int:
    print_banner(cycle)

    t0       = time.time()
    ts_now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    markets: list[MarketData] = []
    snap_rows: list[tuple]    = []   # bulk-insert buffer

    raw_list = fetch_active_markets(limit=MARKET_LIMIT)
    for raw in raw_list:
        yes_p, no_p = parse_prices(raw)
        if yes_p is None or no_p is None:
            continue
        mid = str(raw.get("id") or raw.get("conditionId") or "")
        if not mid:
            continue
        md = MarketData(
            question  = raw.get("question", mid),
            market_id = mid,
            yes_price = yes_p,
            no_price  = no_p,
        )
        markets.append(md)
        snap_rows.append((ts_now, mid, md.question[:120],
                          yes_p, no_p, md.total, md.edge))

    fetch_ms = (time.time() - t0) * 1000
    _latency_ring.append(fetch_ms)

    if not markets:
        print(f"  {RED}[ERROR] 有効な市場データなし（APIタイムアウトの可能性）{RESET}")
        return 0

    # ── Batch-insert all snapshots in ONE transaction ──────────────
    save_snapshots_bulk(snap_rows)

    # ── Prune stale market IDs and update question snapshot ──────────
    # Retired market IDs grow _price_history and _prev_prices unboundedly without pruning.
    global _last_questions
    active_ids = {md.market_id for md in markets}
    for sid in set(_price_history.keys()) - active_ids:
        del _price_history[sid]
    for sid in set(_prev_prices.keys()) - active_ids:
        del _prev_prices[sid]
    # Refresh snapshot for 24h AI refresh trigger (avoids extra API call)
    _last_questions = [md.question for md in markets]

    # ── Build related-pair ID set from AI analyst results ─────────
    # Stores directed tuple (id_broader, id_narrower):
    #   "market_a" = broader (implied, e.g. BTC>$76k)
    #   "market_b" = narrower (implicant, e.g. BTC>$82k) — "B implies A"
    # Direction is preserved to avoid false-positive violation detection.
    q_to_id = {md.question: md.market_id for md in markets}
    related_id_pairs: set[tuple[str, str]] = set()
    with _ai_pairs_lock:
        current_ai_pairs = list(_ai_pairs)
    for pair in current_ai_pairs:
        id_a = q_to_id.get(pair.get("market_a", ""))  # broader
        id_b = q_to_id.get(pair.get("market_b", ""))  # narrower
        if id_a and id_b and id_a != id_b:
            related_id_pairs.add((id_a, id_b))

    # ── Market scan: edge detection first-pass ────────────────────
    # Pre-build token_id map once (clob IDs come from raw Gamma API data)
    raw_by_id   = {str(r.get("id") or r.get("conditionId") or ""): r
                   for r in raw_list if r.get("id") or r.get("conditionId")}

    arb_found   = 0
    edge_mkts   = []
    ph13_positions: list[dict] = []   # for distribute_capital()

    for md in markets:
        if not detect_edge(md, EDGE_THRESHOLD):
            if VERBOSE_MARKETS:
                ev  = calculate_ev(md.yes_price, md.no_price)
                pos = optimal_position_size(md.yes_price, capital=CAPITAL_JPY)
                print_market(markets.index(md) + 1, md, ev, pos)
                print()
            continue

        # ── Edge detected ──────────────────────────────────────────
        ev         = calculate_ev(md.yes_price, md.no_price)
        pos        = optimal_position_size(md.yes_price, capital=CAPITAL_JPY)
        latency_ms = simulate_latency()
        expired    = latency_ms > LATENCY_EXPIRY_MS
        sim        = simulate_trade(md.yes_price, md.no_price, CAPITAL_JPY, ev["best_side"])
        ev_jpy     = 0.0 if expired else sim["expected_value"]
        prev       = _prev_prices.get(md.market_id, md.yes_price)
        pch        = (md.yes_price - prev) / prev if prev else 0.0

        # ── Phase 13: order book depth (non-blocking, best-effort) ─
        depth: DepthSnapshot | None = None
        if PHASE13_DEPTH_ENABLED:
            raw_mkt  = raw_by_id.get(md.market_id, {})
            token_id = parse_clob_token_id(raw_mkt)
            if token_id:
                depth = fetch_depth(token_id)

        # Slippage-adjusted EV at Phase 13 capital scale
        eff_ev = calculate_effective_ev(
            md.yes_price, md.no_price,
            order_size       = PHASE13_CAPITAL_JPY / max(md.yes_price, 0.01),
            liquidity_depth  = depth.ask_depth if depth else None,
        )

        exp_label = (f"{RED}EXPIRED {latency_ms:.0f}ms{RESET}"
                     if expired else f"{GREEN}{latency_ms:.0f}ms OK{RESET}")

        print_market(len(edge_mkts) + 1, md, ev, pos)
        alert(
            f"{md.question[:48]}  SUM={md.total:.4f}  "
            f"EV=¥{ev_jpy:+,.0f}  {ev['best_side']}  {exp_label}"
        )
        if depth:
            print(f"      {CYAN}[Ph13] {depth_summary(depth, PHASE13_CAPITAL_JPY, md.yes_price)}{RESET}")
            if not eff_ev["effective_best_ev"] > 0:
                print(f"      {YELLOW}[Ph13] ⚠ スリッページ後EV負転: 大口注文非推奨{RESET}")
        else:
            print(f"      {YELLOW}[Ph13] 板データなし — スリッページ未評価{RESET}")

        log_virtual_trade(md, ev["best_side"], sim, latency_ms)
        save_edge_event(md.market_id, md.question,
                        md.yes_price, md.no_price,
                        sim["expected_value"], ev["best_side"],
                        latency_ms, expired, price_change_rate=pch)
        _notify_bg(notify_edge_anomaly, md.question, md.edge, ev_jpy,
                   ev["best_side"], price_change_rate=pch)

        # Accumulate for Phase 13 distribution calculation
        ph13_positions.append({
            "market_id": md.market_id,
            "question":  md.question,
            "best_side": ev["best_side"],
            "yes_price": md.yes_price,
            "kelly_f":   pos.get("kelly_f", 0.0),
            "ev":        eff_ev["effective_best_ev"],
        })

        status_txt = "失効（Expired）→ 損益不計上" if expired else "仮想エントリー記録済"
        print(f"      {GREEN}→ {status_txt}{RESET}\n")
        edge_mkts.append(md)
        arb_found += 1

    # ── Phase 13: multi-account distribution plan (when edges found) ─
    if ph13_positions:
        allocs = distribute_capital(
            PHASE13_CAPITAL_JPY, ph13_positions,
            num_accounts=PHASE13_NUM_ACCOUNTS,
        )
        summary = distribution_summary(allocs)
        print(f"\n  {CYAN}[Ph13 配分シミュレーション] {summary}{RESET}")
        for a in allocs:
            warn = f"  {YELLOW}⚠ 大口{RESET}" if a["impact_warning"] else ""
            print(f"    口座{a['account_idx']} | ¥{a['capital_jpy']:>12,.0f} | "
                  f"{a['best_side']:3s} | {a['question'][:45]}{warn}")
        logging.info(f"PH13_DISTRIBUTION | {summary} | positions={len(allocs)}")

    # ── Compact cycle summary ─────────────────────────────────────
    cycle_ms = (time.time() - t0) * 1000
    print(
        f"  市場スキャン: {len(markets)}件  │  "
        f"エッジ: {len(edge_mkts)}件  │  "
        f"AI関連ペア: {len(related_id_pairs)}組  │  "
        f"fetch={fetch_ms:.0f}ms  cycle={cycle_ms:.0f}ms"
    )

    # ── Logical inconsistency sweep (AI-identified pairs only) ────
    # Only check in the CORRECT direction: (id_broader, id_narrower).
    # Violation = narrower market YES price > broader market YES price.
    if related_id_pairs:
        id_to_md = {md.market_id: md for md in markets}
        checked: set[tuple[str, str]] = set()
        for id_broader, id_narrower in related_id_pairs:
            if (id_broader, id_narrower) in checked:
                continue
            checked.add((id_broader, id_narrower))
            ma = id_to_md.get(id_broader)
            mb = id_to_md.get(id_narrower)
            if not ma or not mb:
                continue
            arb = check_logical_inconsistency(ma, mb)
            if arb:
                arb_found += 1
                alert(f"論理矛盾(手数料控除後): {arb['strategy']}")
                logging.info(f"ARB | {json.dumps(arb, ensure_ascii=False)}")

    # ── Triangle arbitrage scan (related pairs only, fee-adjusted) ─
    triangles = scan_triangle_arb(markets, min_compound=0.005,
                                   related_id_pairs=related_id_pairs)
    for tri in triangles[:3]:
        arb_found += 1
        alert(f"三角裁定: {tri['strategy'][:78]}")
        logging.info(
            f"TRIANGLE | chain={tri['chain']} | profit={tri['compound_profit']:.4f}"
        )
        _notify_bg(notify_triangle_anomaly, tri["chain"], tri["prices"],
                   tri["compound_profit"])

    if arb_found == 0:
        print(f"  {GREEN}✓ アービトラージ機会なし{RESET}")

    # ── Volatility singularity check ──────────────────────────────
    check_volatility_singularity(markets, cycle)

    # ── Update previous price snapshot ───────────────────────────
    for md in markets:
        _prev_prices[md.market_id] = md.yes_price

    print_stats_panel()
    return len(markets)


def main():
    print(f"{CYAN}[VILLE v3.1] 起動 — DB初期化中...{RESET}")
    init_db()

    # ── First market fetch (fast) ─────────────────────────────────
    raw = fetch_active_markets(limit=MARKET_LIMIT)
    if not raw:
        print(f"{RED}[VILLE] 市場取得失敗。終了。{RESET}")
        sys.exit(1)
    market_count = len(raw)

    # ── Background AI pair detection (non-blocking startup) ───────
    questions = [m.get("question", "") for m in raw if m.get("question")]
    threading.Thread(
        target=_refresh_ai_pairs_bg, args=(questions,), daemon=True
    ).start()
    print(f"{CYAN}[AI] 論理依存ペア検出をバックグラウンドで開始...{RESET}")

    # ── Background web sync (GitHub Pages auto-update) ────────────
    threading.Thread(target=_web_sync_worker, daemon=True).start()
    print(f"{CYAN}[WEB] GitHub Pages自動更新: {WEB_SYNC_INTERVAL_SEC}秒間隔{RESET}")

    # ── Notification channel status ───────────────────────────────
    channels = channels_active()
    if channels:
        ch_str = "/".join(channels)
        print(f"\n{GREEN}[通知] 有効チャネル: {ch_str}{RESET}")
        _notify_bg(notify_startup, market_count)
    else:
        print(f"\n{YELLOW}[通知] チャネル未設定 — 通知は無効{RESET}")
        print_notification_guide()

    print(f"\n[VILLE] 監視市場数    : {market_count}")
    print(f"[VILLE] サイクル間隔  : {POLL_INTERVAL}秒")
    print(f"[VILLE] ハートビート  : {HEARTBEAT_INTERVAL_CYCLES}サイクル毎 (~1時間)")
    print(f"[VILLE] ボラ特異点    : {VOLATILITY_THRESHOLD:.0%}以上/{VOLATILITY_WINDOW_CYCLES}cyc毎で検知")
    print(f"[VILLE] 日次メンテ    : {MAINTENANCE_INTERVAL_CYCLES}サイクル毎 (~24時間)\n")

    cycle          = 0
    last_mkt_count = market_count

    while True:
        cycle += 1
        t_cycle_start = time.time()
        try:
            result = run_cycle(cycle)
            if result > 0:
                last_mkt_count = result
        except KeyboardInterrupt:
            raise
        except Exception as e:
            import traceback, sys
            tb_str = traceback.format_exc()
            print(f"  {RED}[ERROR] サイクル#{cycle} 例外: {e} — 次サイクルで再試行{RESET}")
            sys.stderr.write(f"CYCLE_ERROR cycle={cycle}: {e}\n{tb_str}\n")
            sys.stderr.flush()
            logging.error(f"CYCLE_ERROR | cycle={cycle} | {e}\n{tb_str}")

        # ── Heartbeat (every ~1 hour) ─────────────────────────────
        if cycle % HEARTBEAT_INTERVAL_CYCLES == 0:
            write_heartbeat(last_mkt_count)

        # ── Daily self-maintenance ────────────────────────────────
        if cycle % MAINTENANCE_INTERVAL_CYCLES == 0:
            do_daily_maintenance()

        # ── Periodic AI pair refresh (every ~24h, background) ────────
        # Uses _last_questions (updated each cycle) — no extra API call needed.
        # SQLite cache handles 24h TTL; this trigger ensures in-memory _ai_pairs
        # is refreshed after new markets appear (e.g. new BTC threshold listings).
        if cycle % AI_REFRESH_INTERVAL_CYCLES == 0 and _last_questions:
            threading.Thread(
                target=_refresh_ai_pairs_bg, args=(_last_questions,), daemon=True
            ).start()

        # ── Adaptive sleep: subtract cycle execution time ─────────
        elapsed = time.time() - t_cycle_start
        sleep_sec = max(0.5, POLL_INTERVAL - elapsed)
        time.sleep(sleep_sec)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{CYAN}[VILLE] 終了。最終統計: {stats.summary_line()}{RESET}")
        sys.exit(0)
