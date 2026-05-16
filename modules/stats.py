"""stats.py — Real-time trade statistics: win rate, drawdown, cumulative P&L"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TradeRecord:
    timestamp:   str
    market:      str
    side:        str
    entry_price: float
    capital:     float
    pnl:         float
    is_win:      bool


@dataclass
class Stats:
    trades: list[TradeRecord] = field(default_factory=list)

    def add(self, trade: TradeRecord):
        self.trades.append(trade)

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def win_count(self) -> int:
        return sum(1 for t in self.trades if t.is_win)

    @property
    def win_rate(self) -> float:
        return (self.win_count / self.total * 100) if self.total else 0.0

    @property
    def cumulative_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def max_drawdown(self) -> float:
        equity, peak, max_dd = 0.0, 0.0, 0.0
        for t in self.trades:
            equity += t.pnl
            peak    = max(peak, equity)
            max_dd  = max(max_dd, peak - equity)
        return max_dd

    @property
    def profit_factor(self) -> float:
        gross_win  = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        return gross_win / gross_loss if gross_loss > 0 else float("inf")

    def summary_line(self) -> str:
        return (
            f"取引数={self.total}  勝率={self.win_rate:.1f}%  "
            f"累計損益=¥{self.cumulative_pnl:,.0f}  "
            f"最大DD=¥{self.max_drawdown:,.0f}  "
            f"PF={self.profit_factor:.2f}"
        )

    def last_n(self, n: int = 5) -> list[TradeRecord]:
        return self.trades[-n:]
