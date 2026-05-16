#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║  PROJECT VILLE — Research & Backtest Engine v1.0                  ║
║  Re-simulate historical snapshots under variable conditions       ║
╚══════════════════════════════════════════════════════════════════╝

Usage:
  python3 ville_research.py              # full report
  python3 ville_research.py --trend      # trend analysis only
  python3 ville_research.py --backtest   # backtest scenarios only
"""

import argparse

from modules.database  import init_db, load_all_snapshots, get_trend_report, DB_PATH

# ── Scenario matrix ───────────────────────────────────────────────
SCENARIOS: list[dict] = [
    {"label": "現行設定    (fee=2%, lat≤2000ms, edge≥2%)",
     "fee": 0.02, "max_lat": 2000, "edge_th": 0.02},
    {"label": "手数料半額  (fee=1%, lat≤2000ms, edge≥2%)",
     "fee": 0.01, "max_lat": 2000, "edge_th": 0.02},
    {"label": "低レイテンシ(fee=2%, lat≤500ms,  edge≥2%)",
     "fee": 0.02, "max_lat":  500, "edge_th": 0.02},
    {"label": "緩和閾値    (fee=2%, lat≤2000ms, edge≥1%)",
     "fee": 0.02, "max_lat": 2000, "edge_th": 0.01},
    {"label": "理想環境    (fee=1%, lat≤500ms,  edge≥1%)",
     "fee": 0.01, "max_lat":  500, "edge_th": 0.01},
]
CAPITAL = 100_000
LAT_MEAN_MS = 800
LAT_STD_MS  = 600


def simulate_ev(yes_price: float, side: str, fee: float) -> float:
    """Re-calculate EV for a snapshot row under given fee."""
    entry = yes_price if side == "YES" else (1.0 - yes_price)
    net_in = CAPITAL * (1 - fee)
    shares = net_in / entry
    win_net  = shares * 1.0 - CAPITAL
    lose_net = -CAPITAL
    return win_net * entry + lose_net * (1.0 - entry)


def run_backtest(snapshots: list[dict]) -> list[dict]:
    """Run all scenarios against historical snapshots. Returns comparison table."""
    import random
    random.seed(42)   # reproducible

    results = []
    for sc in SCENARIOS:
        triggered = 0
        ev_total  = 0.0
        expired   = 0
        wins      = 0

        for row in snapshots:
            edge = (row["yes_price"] + row["no_price"]) - 1.0
            # Edge detection
            if edge > -(sc["edge_th"]):
                continue

            triggered += 1
            side = "YES" if row["yes_price"] < row["no_price"] else "NO"
            ev   = simulate_ev(row["yes_price"], side, sc["fee"])

            # Latency simulation (fixed seed for reproducibility)
            lat = max(50.0, random.gauss(LAT_MEAN_MS, LAT_STD_MS))
            if lat > sc["max_lat"]:
                expired += 1
            else:
                ev_total += ev
                if ev > 0:
                    wins += 1

        valid = triggered - expired
        results.append({
            "label":      sc["label"],
            "triggered":  triggered,
            "expired":    expired,
            "valid":      valid,
            "wins":       wins,
            "win_rate":   (wins / valid * 100) if valid else 0.0,
            "total_ev":   ev_total,
            "avg_ev":     ev_total / valid if valid else 0.0,
        })

    return results


def print_backtest_table(results: list[dict]):
    W = 46
    print(f"\n{'='*110}")
    print("  バックテスト シナリオ比較表  (SQLite蓄積データ使用)")
    print(f"{'='*110}")
    print(f"  {'シナリオ':<{W}}  {'発火':>5}  {'失効':>5}  {'有効':>5}  "
          f"{'勝率':>7}  {'累計EV':>12}  {'平均EV':>10}")
    print(f"  {'-'*W}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*7}  {'─'*12}  {'─'*10}")
    for r in results:
        wr_s = f"{r['win_rate']:.1f}%"
        ev_s = f"¥{r['total_ev']:+,.0f}"
        avg_s = f"¥{r['avg_ev']:+,.0f}"
        print(f"  {r['label']:<{W}}  {r['triggered']:>5}  {r['expired']:>5}  "
              f"{r['valid']:>5}  {wr_s:>7}  {ev_s:>12}  {avg_s:>10}")
    print(f"{'='*110}")


def print_trend_report(report: dict):
    BAR_W = 30
    max_abs = lambda lst, k: max((abs(r.get(k) or 0) for r in lst), default=1) or 1

    print(f"\n{'='*70}")
    print("  [時間帯別] 平均エッジ傾向 (UTC時間)")
    print(f"{'='*70}")
    if not report["by_hour"]:
        print("  データ蓄積中...")
    else:
        ma = max_abs(report["by_hour"], "avg_edge")
        for r in report["by_hour"]:
            v    = r["avg_edge"] or 0
            bars = int(abs(v) / ma * BAR_W)
            bar  = "█" * bars
            col  = "\033[91m" if v < 0 else "\033[92m"
            rst  = "\033[0m"
            print(f"  {r['hour']:02d}:00  {col}{bar:<{BAR_W}}{rst}  "
                  f"avg={v:+.5f}  n={r['snap_count']}")

    print(f"\n{'='*70}")
    print("  [曜日別] 平均エッジ傾向")
    print(f"{'='*70}")
    if not report["by_weekday"]:
        print("  データ蓄積中...")
    else:
        ma = max_abs(report["by_weekday"], "avg_edge")
        for r in report["by_weekday"]:
            v    = r["avg_edge"] or 0
            bars = int(abs(v) / ma * BAR_W)
            bar  = "█" * bars
            col  = "\033[91m" if v < 0 else "\033[92m"
            rst  = "\033[0m"
            print(f"  {r['day_name']}  {col}{bar:<{BAR_W}}{rst}  "
                  f"avg={v:+.5f}  n={r['snap_count']}")

    print(f"\n{'='*70}")
    print("  [ジャンル別] エッジ統計")
    print(f"{'='*70}")
    if not report["by_genre"]:
        print("  データ蓄積中...")
    else:
        print(f"  {'ジャンル':<12}  {'件数':>6}  {'平均edge':>10}  {'最小edge':>10}  {'最大edge':>10}")
        print(f"  {'─'*12}  {'─'*6}  {'─'*10}  {'─'*10}  {'─'*10}")
        for r in report["by_genre"]:
            print(f"  {r['genre']:<12}  {r['count']:>6}  "
                  f"{r['avg_edge']:>+10.5f}  {r['min_edge']:>+10.5f}  {r['max_edge']:>+10.5f}")

    total = report.get("snapshot_total", 0)
    trades = report.get("trade_total", 0)
    print(f"\n  DB統計: スナップショット={total}件 | 仮想取引={trades}件 | DB={DB_PATH}")


def main():
    parser = argparse.ArgumentParser(description="VILLE Research Engine")
    parser.add_argument("--trend",    action="store_true", help="Trend analysis only")
    parser.add_argument("--backtest", action="store_true", help="Backtest scenarios only")
    args = parser.parse_args()

    print(f"[VILLE Research] 起動  DB={DB_PATH}")
    init_db()

    snapshots = load_all_snapshots()
    report    = get_trend_report()

    do_trend    = args.trend    or not (args.trend or args.backtest)
    do_backtest = args.backtest or not (args.trend or args.backtest)

    print(f"[VILLE Research] スナップショット={len(snapshots)}件  "
          f"トレンドデータ={report['snapshot_total']}件")

    if do_trend:
        print_trend_report(report)

    if do_backtest:
        if not snapshots:
            print("\n  [INFO] バックテスト用スナップショットデータなし。")
            print("  ville_main.py または ville_dashboard.py を実行してデータを蓄積してください。")
        else:
            bt_results = run_backtest(snapshots)
            print_backtest_table(bt_results)
            results = bt_results

            # Insight summary
            best = max(bt_results, key=lambda r: r["total_ev"])
            print(f"\n  最優秀シナリオ: {best['label']}")
            print(f"  → 累計EV=¥{best['total_ev']:+,.0f}  "
                  f"勝率={best['win_rate']:.1f}%  有効={best['valid']}件\n")


if __name__ == "__main__":
    main()
