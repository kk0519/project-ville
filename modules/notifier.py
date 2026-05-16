"""notifier.py — Multi-channel anomaly notification: Discord Webhook / LINE Messaging API / SMTP
LINE Notify is discontinued since 2025-03. Use LINE Messaging API instead.
"""
import os
import smtplib
from email.mime.text import MIMEText

import requests

# ── Channel credentials (read from environment) ───────────────────
DISCORD_WEBHOOK      = os.environ.get("DISCORD_WEBHOOK_URL", "")

# LINE Messaging API (replaces discontinued LINE Notify)
# Setup: https://developers.line.biz/ → Create provider → Create Messaging API channel
LINE_CHANNEL_TOKEN   = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID         = os.environ.get("LINE_USER_ID", "")  # target user's LINE user ID

SMTP_TO              = os.environ.get("VILE_ALERT_EMAIL", "")
SMTP_FROM            = os.environ.get("VILE_SMTP_FROM", "")
SMTP_PASS            = os.environ.get("VILE_SMTP_PASS", "")
SMTP_HOST            = os.environ.get("VILE_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT            = int(os.environ.get("VILE_SMTP_PORT", "587"))
TRIANGLE_MIN_PROFIT  = 0.02   # notify triangle arb only if compound profit ≥ 2%

_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def _discord(msg: str) -> bool:
    if not DISCORD_WEBHOOK:
        return False
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
        return r.status_code in (200, 204)
    except Exception:
        return False


def _line(msg: str) -> bool:
    """LINE Messaging API push message (LINE Notify discontinued March 2025)."""
    if not (LINE_CHANNEL_TOKEN and LINE_USER_ID):
        return False
    try:
        r = requests.post(
            _LINE_PUSH_URL,
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
                "Content-Type":  "application/json",
            },
            json={
                "to": LINE_USER_ID,
                "messages": [{"type": "text", "text": msg[:4999]}],
            },
            timeout=5,
        )
        return r.status_code == 200
    except Exception:
        return False


def _email(subject: str, body: str) -> bool:
    if not (SMTP_TO and SMTP_FROM and SMTP_PASS):
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = SMTP_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
        return True
    except Exception:
        return False


def _broadcast(discord_msg: str, line_msg: str,
               email_subject: str, email_body: str) -> bool:
    """Try each channel in priority order; return True if any succeeded."""
    return _discord(discord_msg) or _line(line_msg) or _email(email_subject, email_body)


def notify_edge_anomaly(market: str, edge: float, ev_jpy: float,
                        side: str, price_change_rate: float = 0.0) -> bool:
    """Fire when SUM ≤ (1 − threshold) — standard edge anomaly."""
    title = "⚡ VILLE EDGE ANOMALY"
    body  = (
        f"Market : {market[:60]}\n"
        f"Edge   : {edge:+.4f}  |  Side : {side}\n"
        f"EV(JPY): ¥{ev_jpy:+,.0f}\n"
        f"ΔPrice : {price_change_rate:+.2%}"
    )
    return _broadcast(
        f"**{title}**\n```\n{body}\n```",
        f"\n{title}\n{body}",
        title, body,
    )


def notify_triangle_anomaly(chain: list[str], prices: list[float],
                             compound_profit: float) -> bool:
    """Fire when triangle compound profit ≥ TRIANGLE_MIN_PROFIT."""
    if compound_profit < TRIANGLE_MIN_PROFIT:
        return False
    title = "🔺 VILLE TRIANGLE ARB"
    body  = (
        f"Chain  : {' → '.join(c[:25] for c in chain)}\n"
        f"Prices : {' → '.join(f'{p:.4f}' for p in prices)}\n"
        f"Profit : {compound_profit:.4f}/unit  ({compound_profit * 100:.2f}%)"
    )
    return _broadcast(
        f"**{title}**\n```\n{body}\n```",
        f"\n{title}\n{body}",
        title, body,
    )


def notify_volatility_singularity(market: str, change_pct: float,
                                   yes_price: float) -> bool:
    """Fire when YES price moves ≥3% in ~5 minutes."""
    direction = "急騰🔴" if change_pct > 0 else "急落🔵"
    title = f"⚠️ VILLE VOLATILITY {direction}"
    body  = (
        f"Market : {market[:60]}\n"
        f"Change : {change_pct:+.2%} in ~5 min\n"
        f"YES    : {yes_price:.4f}"
    )
    return _broadcast(
        f"**{title}**\n```\n{body}\n```",
        f"\n{title}\n{body}",
        title, body,
    )


def notify_startup(market_count: int) -> bool:
    """Send one-time startup heartbeat to verify channel connectivity."""
    channels = channels_active()
    ch_str   = "/".join(channels) if channels else "none"
    title = "🚀 VILLE 哨戒任務を開始しました"
    body  = (
        f"監視市場数    : {market_count}\n"
        f"通知チャネル  : {ch_str}\n"
        f"Status        : 正常稼働中"
    )
    return _broadcast(
        f"**{title}**\n```\n{body}\n```",
        f"\n{title}\n{body}",
        title, body,
    )


def channels_active() -> list[str]:
    """Return names of configured notification channels."""
    ch = []
    if DISCORD_WEBHOOK:                    ch.append("Discord")
    if LINE_CHANNEL_TOKEN and LINE_USER_ID: ch.append("LINE(MessagingAPI)")
    if SMTP_TO:                            ch.append("Email")
    return ch
