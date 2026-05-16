#!/usr/bin/env python3
"""ville_web.py — VILLE Web Dashboard (Flask)
Separate process from ville_main.py.

Usage (WSL bash, /mnt/c/Vile directory):
  python3 ville_web.py
  Open: http://localhost:5000
"""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

DB_PATH  = Path(__file__).parent / "ville_data.db"
LOG_PATH = Path(__file__).parent / "ville_backtest.log"

CURRENT_PHASE = 10

# ── Milestone definitions (Phase 1–13) ───────────────────────────
MILESTONES = [
    {
        "phase": 1,
        "emoji": "📐",
        "title": "プロジェクト立ち上げ・アーキテクチャ設計",
        "summary": "Polymarket予測市場監視エンジンの全体構造を設計。モジュール分割・データフロー・DB設計を確定。",
        "tasks": [
            ("要件定義・監視対象（Polymarket）の選定", True),
            ("モジュール構成設計（fetcher/detector/calculator/stats/notifier）", True),
            ("SQLiteスキーマ設計（market_snapshots/edge_events/virtual_trades）", True),
            ("開発環境整備（Python3/pip/Flask/SQLite/DeepSeek API）", True),
        ],
    },
    {
        "phase": 2,
        "emoji": "🌐",
        "title": "データ取得基盤（fetcher.py）",
        "summary": "Polymarket Gamma APIからリアルタイム市場データを取得するモジュールを構築。リトライ・エラー処理込み。",
        "tasks": [
            ("Gamma API（gamma-api.polymarket.com）の仕様調査・検証", True),
            ("fetch_active_markets()：アクティブ市場一括取得実装", True),
            ("parse_prices()：outcomePrices JSONパース・YES/NO価格抽出", True),
            ("数値ID（id）とconditionId（hex）の識別ロジック確立", True),
        ],
    },
    {
        "phase": 3,
        "emoji": "🔍",
        "title": "エッジ検知エンジン（detector.py）",
        "summary": "YES+NOの合計が1を下回る市場（歪み）を検知し、期待値（EV）を計算するコアエンジン。",
        "tasks": [
            ("MarketDataクラス設計（total/edgeプロパティ）", True),
            ("detect_edge()：SUM≤0.98のエッジ検知ロジック", True),
            ("calculate_ev()：手数料2%を考慮した期待値計算", True),
            ("scan_triangle_arb()：AI識別ペアに限定した三角裁定スキャン", True),
            ("check_logical_inconsistency()：論理的矛盾検知（手数料控除後）", True),
        ],
    },
    {
        "phase": 4,
        "emoji": "💹",
        "title": "取引シミュレーション（calculator.py）",
        "summary": "仮想取引（バーチャルBet）をシミュレートし、ケリー基準に基づく最適Bet額を算出するモジュール。",
        "tasks": [
            ("simulate_trade()：仮想取引実行・PnL計算", True),
            ("ケリー基準（Kelly Criterion）によるBet額算出", True),
            ("仮想取引結果のSQLite永続化", True),
        ],
    },
    {
        "phase": 5,
        "emoji": "🗄️",
        "title": "統計・永続化（stats.py + database.py）",
        "summary": "全市場スナップショット・エッジイベント・仮想取引をSQLiteに自動保存。勝率・期待値の統計算出。",
        "tasks": [
            ("database.py：SQLiteスキーマ作成・CRUD操作", True),
            ("stats.py：勝率・EV合計・市場別統計の集計クエリ", True),
            ("市場スナップショットの定期保存（全サイクル自動保存）", True),
        ],
    },
    {
        "phase": 6,
        "emoji": "🤖",
        "title": "AI解析エンジン（ai_analyst.py）",
        "summary": "DeepSeek APIを使い、論理的関連市場ペアの自動識別とエッジ異常の事後分析を実装。24時間キャッシュ付き。",
        "tasks": [
            ("DeepSeek API連携・プロンプト設計", True),
            ("find_related_pairs()：論理的関連市場ペアの自動識別", True),
            ("analyze_anomaly()：エッジ異常の事後AI分析（10分後自動実行）", True),
            ("24時間キャッシュによる不要API呼び出し削減", True),
            ("ヒューリスティックフォールバック（API障害時の代替ロジック）", True),
        ],
    },
    {
        "phase": 7,
        "emoji": "📐",
        "title": "裁定スキャン高度化",
        "summary": "三角裁定・論理的矛盾・イベントクラスター3種の裁定スキャンを完成。related_id_pairsによる誤検知ゼロ化。",
        "tasks": [
            ("scan_triangle_arb()：AI識別済みペアのみ対象に三角裁定を限定", True),
            ("scan_event_cluster()：イベントキーワード別クラスター全組み合わせスキャン", True),
            ("手数料2%/legを全計算に組み込み・偽陽性排除", True),
            ("related_id_pairsがNone/空の場合に即空リスト返却（安全ガード）", True),
        ],
    },
    {
        "phase": 8,
        "emoji": "🔔",
        "title": "通知・異常分析（notifier.py）",
        "summary": "Discord・LINE・SMTPによるエッジ検知通知。AI事後分析（10分後）の自動実行・DB保存・通知送信。",
        "tasks": [
            ("Discord Webhook通知実装（エッジ検知時即時通知）", True),
            ("LINE Notify・SMTP通知の設定対応", True),
            ("AI事後分析の非同期10分後自動実行", True),
            ("anomaly_analysis テーブルへの分析結果永続化", True),
        ],
    },
    {
        "phase": 9,
        "emoji": "⚙️",
        "title": "長期稼働堅牢化（ville_main.py v3.1）",
        "summary": "60分ハートビート・3%/5分ボラティリティ特異点・日次メンテナンスを実装。無人24時間稼働を実現。",
        "tasks": [
            ("60分ごとHEARTBEATログ出力・DB整合性自動チェック", True),
            ("VOLATILITY_SINGULARITY：3%/5分超の価格変動自動検知", True),
            ("日次メンテナンス（00:00 UTC）：古いスナップショット自動削除", True),
            ("_prev_prices辞書による前サイクル比較・価格変動率追跡", True),
            ("gc.collect()による長期稼働時のメモリリーク防止", True),
        ],
    },
    {
        "phase": 10,
        "emoji": "🛠️",
        "title": "無人フォワードテスト（現在地）",
        "summary": "システムを止めずに稼働させ「世界の歪み方」の統計を取る。お金は1円もリスクにさらさない。EAでいうデモ口座フォワードテスト。",
        "tasks": [
            ("24時間連続稼働時のWSL/Pythonメモリ監視（gc.collect()が効いているか）", False),
            ("Discord通知の頻度が適切か（ノイズなく本当に動いた時だけ鳴るか）の確認", False),
            ("経済指標発表時・週末スポーツイベント時の市場の動きとDeepSeek事後推論ログの精査", False),
            ("1週間経過後 ville_research.py --trend で時間帯・ジャンル別「歪み発生傾向」レポート化", False),
        ],
    },
    {
        "phase": 11,
        "emoji": "💻",
        "title": "テスト実弾運用（指先の感覚を研ぎ澄ます）",
        "summary": "Polygonチェーン等のスマートコントラクトへ署名・発注する執行モジュールを開発。資金5〜10万円は約定力とガス代を計測するための授業料。",
        "tasks": [
            ("modules/executor.py（実発注モジュール）の新規設計・実装", False),
            ("Web3ウォレット（秘密鍵）を安全に扱う環境変数・暗号化セキュリティ設定", False),
            ("Polymarket 注文実行APIの叩き込みテスト", False),
            ("1取引あたり3,000〜5,000円の少額実弾でAPI注文・約定テスト", False),
            ("スリッページ計測とロジックへのフィードバック", False),
        ],
    },
    {
        "phase": 12,
        "emoji": "📈",
        "title": "本格実弾運用（ケリー基準の実戦投入）",
        "summary": "システムが自動で資金管理（ケリー基準）を行い、エッジの大きさに合わせて最適ロットを張る真の自動運用フェーズ。資金100〜300万円。",
        "tasks": [
            ("資金残高（Balance）をAPIでリアルタイム取得しケリー基準と完全連動", False),
            ("APIダウン・予期せぬエラー時に全ポジション自動清算または安全ホールドする緊急停止スクリプト配線", False),
            ("利益・損失推移をSQLiteに自動記録しダッシュボードに「資産曲線グラフ」を追加", False),
        ],
    },
    {
        "phase": 13,
        "emoji": "🚀",
        "title": "大資本スケール運用（1,000万円の世界）",
        "summary": "板の厚み監視・複数口座分散により、不眠不休で働く自律型資産運用会社として完成。資金1,000万円。",
        "tasks": [
            ("板の厚み（マーケットデプス）監視機能追加（1,000万円注文による価格影響チェック）", False),
            ("複数アカウントまたは複数取引所への資金分散アルゴリズムの実実装", False),
        ],
    },
]

# ── DB helpers ───────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def get_dashboard_data() -> dict:
    data: dict = {
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "db_ok": DB_PATH.exists(),
        "today": {},
        "edge_today": 0,
        "total_snapshots": 0,
        "total_trades": 0,
        "edge_events": [],
        "anomalies": [],
        "heartbeats": [],
        "last_log_line": "—",
        "volatility_events": [],
    }

    if not DB_PATH.exists():
        return data

    try:
        with _conn() as con:
            r = con.execute("""
                SELECT COUNT(*)                                             AS total,
                       SUM(CASE WHEN is_win=1    THEN 1 ELSE 0 END)       AS wins,
                       SUM(CASE WHEN expired=0   THEN ev_jpy ELSE 0 END)  AS ev_sum,
                       SUM(CASE WHEN expired=1   THEN 1 ELSE 0 END)       AS expired
                FROM virtual_trades WHERE DATE(ts)=DATE('now')
            """).fetchone()
            data["today"] = dict(r) if r else {}

            r = con.execute(
                "SELECT COUNT(*) AS c FROM edge_events WHERE DATE(ts)=DATE('now')"
            ).fetchone()
            data["edge_today"] = r["c"] if r else 0

            data["total_snapshots"] = con.execute(
                "SELECT COUNT(*) AS c FROM market_snapshots"
            ).fetchone()["c"]
            data["total_trades"] = con.execute(
                "SELECT COUNT(*) AS c FROM virtual_trades"
            ).fetchone()["c"]

            rows = con.execute("""
                SELECT ts, question, yes_price, no_price, total, edge,
                       ev_jpy, side, latency_ms, expired, price_change_rate
                FROM edge_events ORDER BY ts DESC LIMIT 15
            """).fetchall()
            data["edge_events"] = [dict(r) for r in rows]

            rows = con.execute("""
                SELECT ts, question, edge, analysis, model
                FROM anomaly_analysis ORDER BY ts DESC LIMIT 8
            """).fetchall()
            data["anomalies"] = [dict(r) for r in rows]

    except Exception as e:
        data["db_error"] = str(e)

    # Parse log file
    if LOG_PATH.exists():
        try:
            lines = LOG_PATH.read_text(errors="replace").splitlines()
            hb = [l for l in lines if "[HEARTBEAT]" in l][-6:]
            data["heartbeats"] = hb

            vol = [l for l in lines if "VOLATILITY_SINGULARITY" in l][-8:]
            data["volatility_events"] = vol

            non_empty = [l for l in reversed(lines) if l.strip()]
            data["last_log_line"] = non_empty[0] if non_empty else "—"
        except Exception:
            pass

    data["pace"] = _get_pace_data()
    return data


def _get_pace_data() -> dict:
    """Compute real-time operating pace metrics from DB."""
    p: dict = {
        "cycles_1h": 0, "cycles_24h": 0,
        "markets_per_cycle": 0,
        "edge_1h": 0, "edge_24h": 0,
        "uptime_h": 0.0, "uptime_label": "—",
        "avg_interval_sec": 0, "last_scan": "—",
    }
    if not DB_PATH.exists():
        return p
    try:
        with _conn() as con:
            # Distinct cycle timestamps in last 1h / 24h
            r = con.execute("""
                SELECT COUNT(DISTINCT ts) AS c1, MAX(ts) AS last
                FROM market_snapshots WHERE ts > datetime('now','-1 hour')
            """).fetchone()
            if r:
                p["cycles_1h"] = r["c1"] or 0
                p["last_scan"] = (r["last"] or "—")[:16]

            r = con.execute("""
                SELECT COUNT(DISTINCT ts) AS c24
                FROM market_snapshots WHERE ts > datetime('now','-24 hours')
            """).fetchone()
            if r:
                p["cycles_24h"] = r["c24"] or 0

            # Average markets monitored per cycle (last 1h)
            r = con.execute("""
                SELECT AVG(cnt) AS avg FROM
                  (SELECT ts, COUNT(*) AS cnt FROM market_snapshots
                   WHERE ts > datetime('now','-1 hour') GROUP BY ts)
            """).fetchone()
            if r and r["avg"]:
                p["markets_per_cycle"] = round(r["avg"])

            # Edge detection rate
            r = con.execute("""
                SELECT COUNT(*) AS c FROM edge_events
                WHERE ts > datetime('now','-1 hour')
            """).fetchone()
            p["edge_1h"] = r["c"] if r else 0

            r = con.execute("""
                SELECT COUNT(*) AS c FROM edge_events
                WHERE ts > datetime('now','-24 hours')
            """).fetchone()
            p["edge_24h"] = r["c"] if r else 0

            # Uptime from first snapshot
            r = con.execute("SELECT MIN(ts) AS first FROM market_snapshots").fetchone()
            if r and r["first"]:
                try:
                    first_str = r["first"].replace("Z", "")
                    first_dt = datetime.fromisoformat(first_str)
                    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
                    sec = (now_dt - first_dt).total_seconds()
                    h = sec / 3600
                    p["uptime_h"] = round(h, 1)
                    if h >= 24:
                        p["uptime_label"] = f"{h/24:.1f}日"
                    elif h >= 1:
                        p["uptime_label"] = f"{h:.1f}時間"
                    else:
                        p["uptime_label"] = f"{int(sec/60)}分"
                except Exception:
                    pass

            # Average interval between cycles (seconds)
            if p["cycles_24h"] > 1:
                p["avg_interval_sec"] = round(86400 / p["cycles_24h"])

    except Exception as e:
        p["error"] = str(e)
    return p


# ── HTML Template ────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚡ VILLE Dashboard</title>
<style>
  :root {
    --bg:      #0d1117;
    --card:    #161b22;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --accent:  #00d4aa;
    --green:   #3fb950;
    --yellow:  #d29922;
    --red:     #ff7b72;
    --orange:  #f0883e;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'Courier New', 'Menlo', monospace;
    font-size: 13px; line-height: 1.6;
  }
  a { color: var(--accent); text-decoration: none; }

  /* Header */
  header {
    background: var(--card); border-bottom: 1px solid var(--border);
    padding: 16px 32px; display: flex; align-items: center; gap: 16px;
  }
  header h1 { font-size: 20px; color: var(--accent); letter-spacing: 2px; }
  header .sub { color: var(--muted); font-size: 11px; }
  header .ts  { margin-left: auto; color: var(--muted); font-size: 11px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         background: var(--green); animation: pulse 2s infinite; margin-right: 6px; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  /* Layout */
  main { max-width: 1400px; margin: 0 auto; padding: 24px 32px; }
  .section-title {
    color: var(--muted); font-size: 11px; letter-spacing: 1px;
    text-transform: uppercase; margin: 28px 0 12px; border-bottom: 1px solid var(--border);
    padding-bottom: 6px;
  }

  /* Stat cards */
  .stat-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px;
  }
  .stat-card {
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 16px 20px;
  }
  .stat-card .label { color: var(--muted); font-size: 11px; margin-bottom: 6px; }
  .stat-card .value { font-size: 26px; font-weight: bold; color: var(--accent); }
  .stat-card .sub   { color: var(--muted); font-size: 11px; margin-top: 4px; }

  /* Phase cards */
  .phase-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px;
  }
  .phase-card {
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 20px;
  }
  .phase-card h3 {
    color: var(--accent); font-size: 13px; margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px;
  }
  .phase-num {
    background: var(--accent); color: #000; border-radius: 4px;
    padding: 1px 7px; font-size: 11px; font-weight: bold;
  }
  .task { display: flex; align-items: flex-start; gap: 8px; padding: 4px 0;
          color: var(--muted); font-size: 12px; }
  .task.done { color: var(--text); }
  .task .icon { flex-shrink: 0; margin-top: 1px; }
  .task.done .icon { color: var(--green); }
  .progress-bar {
    height: 3px; background: var(--border); border-radius: 2px;
    margin: 12px 0 0; overflow: hidden;
  }
  .progress-fill {
    height: 100%; background: var(--accent); border-radius: 2px;
    transition: width .4s;
  }
  .pct { color: var(--muted); font-size: 11px; text-align: right; margin-top: 4px; }

  /* Tables */
  table { width: 100%; border-collapse: collapse; }
  th {
    background: var(--card); color: var(--muted); font-size: 11px;
    text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border);
    letter-spacing: .5px; text-transform: uppercase;
  }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); font-size: 12px; }
  tr:hover td { background: #1c2128; }
  .badge {
    display: inline-block; padding: 1px 8px; border-radius: 20px;
    font-size: 11px; font-weight: bold;
  }
  .badge-yes    { background: #1f4d2e; color: var(--green); }
  .badge-no     { background: #4d1f1f; color: var(--red); }
  .badge-exp    { background: #3d3110; color: var(--yellow); }
  .badge-ok     { background: #1a3a30; color: var(--accent); }
  .edge-neg     { color: var(--red); }
  .edge-pos     { color: var(--green); }

  /* Log lines */
  .log-box {
    background: #0a0e14; border: 1px solid var(--border); border-radius: 8px;
    padding: 16px; max-height: 220px; overflow-y: auto;
  }
  .log-line {
    font-size: 11px; color: var(--muted); padding: 2px 0;
    border-bottom: 1px solid #1a1f26;
  }
  .log-line:last-child { border-bottom: none; color: var(--text); }
  .log-line.hb  { color: var(--accent); }
  .log-line.vol { color: var(--orange); }

  /* Anomaly card */
  .anomaly-card {
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 14px 16px; margin-bottom: 10px;
  }
  .anomaly-card .q  { color: var(--text); margin-bottom: 6px; font-size: 12px; }
  .anomaly-card .ts { color: var(--muted); font-size: 11px; }
  .anomaly-card .body { color: #c9d1d9; font-size: 12px; margin-top: 8px;
                        border-left: 3px solid var(--orange); padding-left: 10px; }

  /* Timeline */
  .timeline-wrap {
    display: flex; align-items: flex-start; gap: 0;
    overflow-x: auto; padding-bottom: 8px;
  }
  .tl-step {
    display: flex; flex-direction: column; align-items: center;
    flex: 1; min-width: 72px; position: relative;
  }
  .tl-step:not(:last-child)::after {
    content: '';
    position: absolute; top: 14px; left: calc(50% + 14px);
    width: calc(100% - 28px); height: 2px;
    background: var(--border);
    z-index: 0;
  }
  .tl-step.done::after  { background: var(--green); }
  .tl-step.current::after { background: var(--accent); }
  .tl-dot {
    width: 28px; height: 28px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: bold; position: relative; z-index: 1;
    border: 2px solid var(--border); background: var(--bg); color: var(--muted);
  }
  .tl-step.done    .tl-dot { border-color: var(--green);  color: var(--green);  background: #0d1f14; }
  .tl-step.current .tl-dot { border-color: var(--accent); color: #000; background: var(--accent); box-shadow: 0 0 10px var(--accent); }
  .tl-step.future  .tl-dot { border-color: var(--border); }
  .tl-label {
    font-size: 10px; margin-top: 6px; text-align: center;
    color: var(--muted); line-height: 1.3; max-width: 72px;
  }
  .tl-step.done    .tl-label { color: var(--green); }
  .tl-step.current .tl-label { color: var(--accent); font-weight: bold; }
  .tl-current-badge {
    display: inline-block; background: var(--accent); color: #000;
    font-size: 10px; font-weight: bold; border-radius: 3px;
    padding: 1px 5px; margin-top: 4px;
  }

  /* No-data */
  .no-data { color: var(--muted); padding: 24px; text-align: center; font-size: 12px; }

  /* Footer */
  footer {
    margin: 40px 32px 24px; color: var(--muted); font-size: 11px;
    border-top: 1px solid var(--border); padding-top: 16px;
    display: flex; justify-content: space-between;
  }
</style>
<script>
  // Auto-refresh every 60 seconds
  let t = 60;
  function tick() {
    t--;
    const el = document.getElementById('countdown');
    if (el) el.textContent = t + 's';
    if (t <= 0) location.reload();
    else setTimeout(tick, 1000);
  }
  window.onload = tick;
</script>
</head>
<body>

<header>
  <div>
    <h1>⚡ PROJECT VILLE</h1>
    <div class="sub">自律哨戒システム — Polymarket予測市場監視エンジン v3.1</div>
  </div>
  <div class="ts">
    <span class="dot"></span>LIVE &nbsp;|&nbsp;
    更新: {{ data.now }} &nbsp;|&nbsp;
    次回リフレッシュ: <span id="countdown">60s</span>
  </div>
</header>

<main>

  <!-- ── Timeline ── -->
  <div class="section-title">全体スケジュール（Phase 1 – 13）</div>
  <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:24px 20px 16px;">
    <div class="timeline-wrap">
      {% for ph in milestones %}
      {% if ph.phase < current_phase %}
      <div class="tl-step done">
      {% elif ph.phase == current_phase %}
      <div class="tl-step current">
      {% else %}
      <div class="tl-step future">
      {% endif %}
        <div class="tl-dot">
          {% if ph.phase < current_phase %}✓{% elif ph.phase == current_phase %}{{ ph.phase }}{% else %}{{ ph.phase }}{% endif %}
        </div>
        <div class="tl-label">
          Ph.{{ ph.phase }}<br>{{ ph.emoji }}<br>{{ ph.title[:14] }}{% if ph.title|length > 14 %}…{% endif %}
          {% if ph.phase == current_phase %}
          <br><span class="tl-current-badge">現在地</span>
          {% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    <div style="margin-top:12px;font-size:11px;color:var(--muted)">
      <span style="color:var(--green)">■ 完了 (1–9)</span> &nbsp;
      <span style="color:var(--accent)">■ 現在地 (Phase {{ current_phase }})</span> &nbsp;
      <span style="color:var(--border)">■ 未着手 ({{ current_phase + 1 }}–13)</span>
    </div>
  </div>

  <!-- ── Operating Pace ── -->
  <div class="section-title">稼働ペース（リアルタイム）</div>
  <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:20px 24px;">
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px">

      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:4px">スキャンサイクル / 時</div>
        <div style="font-size:28px;font-weight:bold;color:var(--accent)">{{ data.pace.cycles_1h }}</div>
        <div style="color:var(--muted);font-size:11px">cycles / hour</div>
      </div>

      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:4px">スキャンサイクル / 日</div>
        <div style="font-size:28px;font-weight:bold;color:var(--accent)">{{ '{:,}'.format(data.pace.cycles_24h) }}</div>
        <div style="color:var(--muted);font-size:11px">cycles / 24h</div>
      </div>

      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:4px">監視市場数 / サイクル</div>
        <div style="font-size:28px;font-weight:bold;color:var(--text)">{{ data.pace.markets_per_cycle }}</div>
        <div style="color:var(--muted);font-size:11px">markets / cycle</div>
      </div>

      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:4px">平均サイクル間隔</div>
        <div style="font-size:28px;font-weight:bold;color:var(--text)">{{ data.pace.avg_interval_sec }}</div>
        <div style="color:var(--muted);font-size:11px">seconds / cycle</div>
      </div>

      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:4px">エッジ検知 / 時</div>
        <div style="font-size:28px;font-weight:bold;
          {% if data.pace.edge_1h > 0 %}color:var(--green){% else %}color:var(--muted){% endif %}">
          {{ data.pace.edge_1h }}
        </div>
        <div style="color:var(--muted);font-size:11px">detections / hour</div>
      </div>

      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:4px">エッジ検知 / 日</div>
        <div style="font-size:28px;font-weight:bold;
          {% if data.pace.edge_24h > 0 %}color:var(--green){% else %}color:var(--muted){% endif %}">
          {{ data.pace.edge_24h }}
        </div>
        <div style="color:var(--muted);font-size:11px">detections / 24h</div>
      </div>

      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:4px">継続稼働時間</div>
        <div style="font-size:28px;font-weight:bold;color:var(--orange)">{{ data.pace.uptime_label }}</div>
        <div style="color:var(--muted);font-size:11px">({{ data.pace.uptime_h }}h 累計)</div>
      </div>

      <div>
        <div style="color:var(--muted);font-size:11px;margin-bottom:4px">最終スキャン</div>
        <div style="font-size:14px;font-weight:bold;color:var(--text);margin-top:8px">{{ data.pace.last_scan }}</div>
        <div style="color:var(--muted);font-size:11px">UTC</div>
      </div>

    </div>
    {% if data.pace.avg_interval_sec > 0 %}
    <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border);
                display:flex;gap:24px;flex-wrap:wrap;font-size:11px;color:var(--muted)">
      <span>📊 24h 合計スキャン数: <strong style="color:var(--text)">{{ '{:,}'.format(data.pace.cycles_24h * (data.pace.markets_per_cycle or 1)) }}</strong> 市場</span>
      <span>⚡ エッジ検知率: <strong style="color:var(--green)">{{ '%.2f' % (data.pace.edge_24h / max(data.pace.cycles_24h, 1) * 100) }}%</strong> / サイクル</span>
      <span>🔄 1分あたり: <strong style="color:var(--text)">{{ '%.1f' % (data.pace.cycles_1h / 60.0) }}</strong> サイクル</span>
    </div>
    {% endif %}
  </div>

  <!-- ── Stats ── -->
  <div class="section-title">稼働ステータス</div>
  <div class="stat-grid">
    <div class="stat-card">
      <div class="label">今日のエッジ検知</div>
      <div class="value">{{ data.edge_today }}</div>
      <div class="sub">edge events / today</div>
    </div>
    <div class="stat-card">
      <div class="label">今日の仮想取引</div>
      <div class="value">{{ data.today.get('total', 0) or 0 }}</div>
      <div class="sub">
        勝率 {{ '%.0f' % ((data.today.get('wins',0) or 0) /
          max(data.today.get('total',1) or 1, 1) * 100) }}%
      </div>
    </div>
    <div class="stat-card">
      <div class="label">今日の累計EV</div>
      <div class="value {% if (data.today.get('ev_sum') or 0) >= 0 %}edge-pos{% else %}edge-neg{% endif %}">
        ¥{{ '{:,.0f}'.format(data.today.get('ev_sum') or 0) }}
      </div>
      <div class="sub">expected value</div>
    </div>
    <div class="stat-card">
      <div class="label">DBスナップショット</div>
      <div class="value">{{ '{:,}'.format(data.total_snapshots) }}</div>
      <div class="sub">market_snapshots rows</div>
    </div>
    <div class="stat-card">
      <div class="label">累計仮想取引数</div>
      <div class="value">{{ '{:,}'.format(data.total_trades) }}</div>
      <div class="sub">virtual_trades rows</div>
    </div>
    <div class="stat-card">
      <div class="label">AI分析件数</div>
      <div class="value">{{ data.anomalies | length }}</div>
      <div class="sub">anomaly_analysis (直近)</div>
    </div>
  </div>

  <!-- ── Roadmap ── -->
  <div class="section-title">ロードマップ進捗（全 Phase 1 – 13）</div>
  <div class="phase-grid">
    {% for ph in milestones %}
    {% set done_cnt = ph.tasks | selectattr(1) | list | length %}
    {% set total_cnt = ph.tasks | length %}
    {% set pct = (done_cnt / total_cnt * 100) | int %}
    <div class="phase-card">
      <h3>
        <span class="phase-num">Ph.{{ ph.phase }}</span>
        {{ ph.emoji }} {{ ph.title }}
      </h3>
      <p style="color:var(--muted);font-size:11px;margin-bottom:12px;line-height:1.7">{{ ph.summary }}</p>
      {% for task, done in ph.tasks %}
      <div class="task {% if done %}done{% endif %}">
        <span class="icon">{% if done %}✅{% else %}⬜{% endif %}</span>
        <span>{{ task }}</span>
      </div>
      {% endfor %}
      <div class="progress-bar">
        <div class="progress-fill" style="width:{{ pct }}%"></div>
      </div>
      <div class="pct">{{ done_cnt }}/{{ total_cnt }} — {{ pct }}%</div>
    </div>
    {% endfor %}
  </div>

  <!-- ── Heartbeat log ── -->
  <div class="section-title">ハートビート履歴（最新6件）</div>
  <div class="log-box">
    {% if data.heartbeats %}
      {% for line in data.heartbeats %}
      <div class="log-line hb">{{ line }}</div>
      {% endfor %}
    {% else %}
      <div class="no-data">ハートビートなし（ville_main.py 未起動 or 60サイクル未満）</div>
    {% endif %}
  </div>

  <!-- ── Volatility events ── -->
  {% if data.volatility_events %}
  <div class="section-title">ボラティリティ特異点検知履歴（最新8件）</div>
  <div class="log-box">
    {% for line in data.volatility_events %}
    <div class="log-line vol">{{ line }}</div>
    {% endfor %}
  </div>
  {% endif %}

  <!-- ── Edge events table ── -->
  <div class="section-title">エッジイベント履歴（直近15件）</div>
  {% if data.edge_events %}
  <div style="overflow-x:auto">
  <table>
    <thead>
      <tr>
        <th>時刻 (UTC)</th>
        <th>市場（先頭55文字）</th>
        <th>YES</th>
        <th>NO</th>
        <th>SUM</th>
        <th>EDGE</th>
        <th>EV(JPY)</th>
        <th>サイド</th>
        <th>レイテンシ</th>
        <th>状態</th>
      </tr>
    </thead>
    <tbody>
      {% for ev in data.edge_events %}
      <tr>
        <td style="white-space:nowrap;color:var(--muted)">{{ ev.ts[:16] }}</td>
        <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          {{ ev.question[:55] }}
        </td>
        <td>{{ '%.4f' % (ev.yes_price or 0) }}</td>
        <td>{{ '%.4f' % (ev.no_price  or 0) }}</td>
        <td class="{% if (ev.total or 1) < 0.98 %}edge-neg{% else %}edge-pos{% endif %}">
          {{ '%.4f' % (ev.total or 0) }}
        </td>
        <td class="edge-neg">{{ '%+.4f' % (ev.edge or 0) }}</td>
        <td class="{% if (ev.ev_jpy or 0) > 0 %}edge-pos{% else %}edge-neg{% endif %}">
          ¥{{ '{:,.0f}'.format(ev.ev_jpy or 0) }}
        </td>
        <td>
          <span class="badge {% if ev.side=='YES' %}badge-yes{% else %}badge-no{% endif %}">
            {{ ev.side }}
          </span>
        </td>
        <td style="color:var(--muted)">{{ '%.0f' % (ev.latency_ms or 0) }}ms</td>
        <td>
          {% if ev.expired %}
          <span class="badge badge-exp">EXPIRED</span>
          {% else %}
          <span class="badge badge-ok">OK</span>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  </div>
  {% else %}
  <div class="no-data">エッジイベントなし（エッジ閾値 SUM≤0.98 の市場が未検出）</div>
  {% endif %}

  <!-- ── AI Anomaly Analyses ── -->
  {% if data.anomalies %}
  <div class="section-title">AI異常分析レポート（直近8件）</div>
  {% for a in data.anomalies %}
  <div class="anomaly-card">
    <div class="ts">{{ a.ts }} &nbsp;|&nbsp; model: {{ a.model }}</div>
    <div class="q">{{ a.question }}</div>
    <div class="body">{{ a.analysis }}</div>
  </div>
  {% endfor %}
  {% endif %}

  <!-- ── Last log line ── -->
  <div class="section-title">最終ログ行</div>
  <div class="log-box">
    <div class="log-line">{{ data.last_log_line }}</div>
  </div>

</main>

<footer>
  <span>PROJECT VILLE v3.1 &nbsp;—&nbsp; Polymarket Simulation Engine</span>
  <span>DB: {{ 'Connected' if data.db_ok else 'Not found' }} &nbsp;|&nbsp; {{ data.now }}</span>
</footer>

</body>
</html>"""


# ── Routes ───────────────────────────────────────────────────────
@app.route("/")
def index():
    data = get_dashboard_data()
    return render_template_string(HTML, milestones=MILESTONES, data=data, current_phase=CURRENT_PHASE)


@app.route("/api/data")
def api_data():
    return jsonify(get_dashboard_data())


if __name__ == "__main__":
    print("=" * 60)
    print("  VILLE Web Dashboard")
    print("  http://localhost:5000")
    print("  Ctrl+C で停止")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)
