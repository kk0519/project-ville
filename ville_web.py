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

# ── Milestone definitions (Phase 10–13) ──────────────────────────
MILESTONES = [
    {
        "phase": 10,
        "title": "長期無人稼働の堅牢化",
        "tasks": [
            ("Polymarket API接続・15市場リアルタイム監視", True),
            ("エッジ検知（SUM≤0.98）アルゴリズム実装", True),
            ("SQLite永続化（snapshots/edge_events/trades）", True),
            ("ハートビート60分間隔ロギング", True),
            ("ボラティリティ特異点検知（3%/5分）", True),
            ("日次セルフメンテナンス（ログローテーション/GC/DBクリーンアップ）", True),
        ],
    },
    {
        "phase": 11,
        "title": "通知チャネル完全統合",
        "tasks": [
            ("Discord Webhook通知", True),
            ("LINE Messaging API移行（LINE Notify廃止対応）", True),
            ("Gmail SMTP通知", True),
            ("スタートアップハートビート通知", True),
            ("エッジ異常・三角裁定・ボラ特異点 3トリガー配線", True),
        ],
    },
    {
        "phase": 12,
        "title": "裁定ロジック厳格化",
        "tasks": [
            ("conditionId→numeric ID修正（全市場フェッチ成功）", True),
            ("DeepSeek AI論理依存ペア検出（24hキャッシュ）", True),
            ("三角裁定：AI判定関連ペアのみに限定", True),
            ("論理矛盾チェック：隣接比較廃止→AI対象ペアのみ", True),
            ("手数料2%控除後純利益計算・99.9%異常値除去", True),
        ],
    },
    {
        "phase": 13,
        "title": "Webダッシュボード",
        "tasks": [
            ("Flask Webサーバー（ville_main.pyと独立プロセス）", True),
            ("ロードマップ進捗チェックリスト表示", True),
            ("SQLiteリアルタイムデータ表示", True),
            ("ハートビート・ボラティリティ履歴表示", True),
            ("ダークモードUI（エンジニアライク）", True),
            ("60秒オートリフレッシュ", True),
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

    return data


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
  <div class="section-title">ロードマップ進捗（Phase 10 – 13）</div>
  <div class="phase-grid">
    {% for ph in milestones %}
    {% set done_cnt = ph.tasks | selectattr(1) | list | length %}
    {% set total_cnt = ph.tasks | length %}
    {% set pct = (done_cnt / total_cnt * 100) | int %}
    <div class="phase-card">
      <h3>
        <span class="phase-num">Ph.{{ ph.phase }}</span>
        {{ ph.title }}
      </h3>
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
    return render_template_string(HTML, milestones=MILESTONES, data=data)


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
