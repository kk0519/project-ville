"""stats.py — Real-time trade statistics: win rate, drawdown, cumulative P&L

Performance design:
- TradeRecord uses slots=True  → eliminates per-instance __dict__
- Stats uses incremental counters (_wins, _pnl_sum) → O(1) for hot properties
- trim() caps memory at max_records trades (called from heartbeat)
"""
from dataclasses import dataclass, field


@dataclass(slots=True)
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
    trades:   list[TradeRecord] = field(default_factory=list)
    # Incremental O(1) counters — updated in add(), reset in trim()
    _wins:    int               = field(default=0,   init=False, repr=False)
    _pnl_sum: float             = field(default=0.0, init=False, repr=False)

    def add(self, trade: TradeRecord):
        self.trades.append(trade)
        if trade.is_win:
            self._wins += 1
        self._pnl_sum += trade.pnl

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def win_count(self) -> int:
        return self._wins              # O(1)

    @property
    def win_rate(self) -> float:
        return (self._wins / len(self.trades) * 100) if self.trades else 0.0

    @property
    def cumulative_pnl(self) -> float:
        return self._pnl_sum           # O(1)

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

    def trim(self, max_records: int = 10_000):
        """Cap in-memory trades list to prevent unbounded growth.

        Called from heartbeat (~1h interval). O(n) one-time cost on trim,
        then O(1) for hot properties until the next trim.
        """
        if len(self.trades) > max_records:
            self.trades   = self.trades[-max_records:]
            self._wins    = sum(1 for t in self.trades if t.is_win)
            self._pnl_sum = sum(t.pnl for t in self.trades)

    def summary_line(self) -> str:
        return (
            f"取引数={self.total}  勝率={self.win_rate:.1f}%  "
            f"累計損益=¥{self._pnl_sum:,.0f}  "
            f"最大DD=¥{self.max_drawdown:,.0f}  "
            f"PF={self.profit_factor:.2f}"
        )

    def last_n(self, n: int = 5) -> list[TradeRecord]:
        return self.trades[-n:]
