#!/usr/bin/env python3
"""Executable SSE order-book pressure strategy and one-day T+1 backtest."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

try:
    import duckdb
except ImportError as exc:
    raise SystemExit("Install duckdb first: python -m pip install duckdb") from exc


def milliseconds(value: int) -> int:
    text = f"{value:09d}"
    return ((int(text[:2]) * 60 + int(text[2:4])) * 60 + int(text[4:6])) * 1000 + int(text[6:])


def in_session(value: int) -> bool:
    return 93_100_000 <= value <= 112_900_000 or 130_100_000 <= value <= 145_500_000


def fee(notional: float, side: str) -> float:
    commission = max(5.0, notional * 0.0003)
    transfer_fee = notional * 0.00001
    stamp_duty = notional * 0.0005 if side == "SELL" else 0.0
    return commission + transfer_fee + stamp_duty


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", type=Path, default=Path("reconstructed_orderbook_20260923.parquet"))
    parser.add_argument("--initial-capital", type=float, default=1_000_000)
    parser.add_argument("--initial-invested-ratio", type=float, default=0.5)
    parser.add_argument("--threshold", type=float, default=0.70)
    parser.add_argument("--mode", choices=("momentum", "contrarian"), default="momentum")
    parser.add_argument("--start-time", type=int, default=93_100_000)
    parser.add_argument("--cooldown-seconds", type=int, default=600)
    parser.add_argument("--max-trades-per-symbol", type=int, default=2)
    parser.add_argument("--trade-lots", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=Path("strategy_output"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    book = str(args.book.resolve()).replace("'", "''")
    fields = ["Symbol", "Time"]
    for level in range(1, 6):
        fields.extend((f"BidPrice{level}", f"BidVolume{level}", f"AskPrice{level}", f"AskVolume{level}"))
    rows = con.execute(
        f"SELECT {','.join(fields)} FROM read_parquet('{book}') "
        "WHERE Time BETWEEN 93000000 AND 150000000 ORDER BY Time, Symbol"
    ).fetchall()

    first_quote = {}
    last_quote = {}
    for row in rows:
        symbol, _, bid, bid_volume, ask, ask_volume = row[:6]
        if bid > 0 and ask > bid and bid_volume >= 100 and ask_volume >= 100:
            first_quote.setdefault(symbol, (bid + ask) / 2)
            last_quote[symbol] = (bid + ask) / 2

    symbols = sorted(first_quote)
    allocation = args.initial_capital * args.initial_invested_ratio / len(symbols)
    positions = {}
    sellable = {}
    initial_positions = {}
    initial_values = {}
    for symbol in symbols:
        price = first_quote[symbol]
        quantity = math.floor(allocation / price / 100) * 100
        positions[symbol] = quantity
        sellable[symbol] = quantity
        initial_positions[symbol] = quantity
        initial_values[symbol] = quantity * price

    invested = sum(initial_values.values())
    cash = args.initial_capital - invested
    initial_cash = cash
    cooldown_ms = args.cooldown_seconds * 1000
    last_trade_ms = defaultdict(lambda: -10**12)
    trade_counts = defaultdict(int)
    trades = []
    signal_rows = []
    lot_size = args.trade_lots * 100
    max_position_value = args.initial_capital * 0.025

    for row in rows:
        symbol, time_value = row[:2]
        if not in_session(time_value) or time_value < args.start_time:
            continue
        values = row[2:]
        bids = [(values[i * 4], values[i * 4 + 1]) for i in range(5)]
        asks = [(values[i * 4 + 2], values[i * 4 + 3]) for i in range(5)]
        bid1, bid_volume1 = bids[0]
        ask1, ask_volume1 = asks[0]
        if bid1 <= 0 or ask1 <= bid1 or bid_volume1 < 100 or ask_volume1 < 100:
            continue
        mid = (bid1 + ask1) / 2
        spread_bps = (ask1 - bid1) / mid * 10_000
        if spread_bps > 10:
            continue

        bid_depth = sum(volume / level for level, (_, volume) in enumerate(bids, 1))
        ask_depth = sum(volume / level for level, (_, volume) in enumerate(asks, 1))
        if bid_depth + ask_depth == 0 or bid_volume1 + ask_volume1 == 0:
            continue
        imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        micro_price = (ask1 * bid_volume1 + bid1 * ask_volume1) / (bid_volume1 + ask_volume1)
        micro_signal = max(-1.0, min(1.0, 2 * (micro_price - mid) / (ask1 - bid1)))
        raw_pressure = 0.7 * imbalance + 0.3 * micro_signal
        score = -raw_pressure if args.mode == "contrarian" else raw_pressure
        action = "HOLD"

        now_ms = milliseconds(time_value)
        can_trade = (
            now_ms - last_trade_ms[symbol] >= cooldown_ms
            and trade_counts[symbol] < args.max_trades_per_symbol
        )
        if can_trade and score >= args.threshold:
            notional = ask1 * lot_size
            trading_fee = fee(notional, "BUY")
            if ask_volume1 >= lot_size and cash >= notional + trading_fee and (positions[symbol] + lot_size) * ask1 <= max_position_value:
                cash -= notional + trading_fee
                positions[symbol] += lot_size
                # T+1: this quantity is deliberately not added to sellable.
                action = "BUY"
                trades.append((time_value, symbol, action, ask1, lot_size, score, imbalance, micro_signal, trading_fee, cash, positions[symbol], sellable[symbol]))
        elif can_trade and score <= -args.threshold:
            notional = bid1 * lot_size
            trading_fee = fee(notional, "SELL")
            if bid_volume1 >= lot_size and sellable[symbol] >= lot_size:
                cash += notional - trading_fee
                positions[symbol] -= lot_size
                sellable[symbol] -= lot_size
                action = "SELL"
                trades.append((time_value, symbol, action, bid1, lot_size, score, imbalance, micro_signal, trading_fee, cash, positions[symbol], sellable[symbol]))

        if action != "HOLD":
            last_trade_ms[symbol] = now_ms
            trade_counts[symbol] += 1
        if abs(score) >= args.threshold:
            signal_rows.append((time_value, symbol, bid1, ask1, spread_bps, imbalance, micro_signal, score, action))

    final_stock_value = sum(positions[symbol] * last_quote[symbol] for symbol in symbols)
    final_nav = cash + final_stock_value
    benchmark_nav = initial_cash + sum(initial_positions[symbol] * last_quote[symbol] for symbol in symbols)
    total_fees = sum(row[8] for row in trades)

    trade_file = args.output_dir / "trades_20260923.csv"
    with trade_file.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(("Time", "Symbol", "Side", "Price", "Quantity", "Score", "Imbalance", "MicroSignal", "Fee", "CashAfter", "PositionAfter", "SellableAfter"))
        writer.writerows(trades)

    signal_file = args.output_dir / "signals_20260923.csv"
    with signal_file.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(("Time", "Symbol", "Bid1", "Ask1", "SpreadBps", "Imbalance", "MicroSignal", "Score", "Action"))
        writer.writerows(signal_rows)

    summary_file = args.output_dir / "strategy_summary_20260923.csv"
    with summary_file.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(("Metric", "Value"))
        writer.writerows((
            ("InitialCapital", args.initial_capital),
            ("InitialCash", initial_cash),
            ("InitialStockValue", invested),
            ("TradeCount", len(trades)),
            ("BuyCount", sum(row[2] == "BUY" for row in trades)),
            ("SellCount", sum(row[2] == "SELL" for row in trades)),
            ("TotalFees", total_fees),
            ("FinalCash", cash),
            ("FinalStockValue", final_stock_value),
            ("FinalNAV", final_nav),
            ("StrategyReturn", final_nav / args.initial_capital - 1),
            ("BenchmarkNAV", benchmark_nav),
            ("BenchmarkReturn", benchmark_nav / args.initial_capital - 1),
            ("ExcessReturn", (final_nav - benchmark_nav) / args.initial_capital),
        ))

    print(f"Trades: {len(trades)}")
    print(f"Final NAV: {final_nav:.2f}")
    print(f"Strategy return: {final_nav / args.initial_capital - 1:.4%}")
    print(f"Benchmark return: {benchmark_nav / args.initial_capital - 1:.4%}")
    print(f"Excess return: {(final_nav - benchmark_nav) / args.initial_capital:.4%}")
    print(f"Output directory: {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
