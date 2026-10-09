#!/usr/bin/env python3
"""Reconstruct SSE ten-level order books from order and execution parquet files.

The generated book is sampled at every timestamp present in the source snapshot
file. Source snapshots are used only for validation, never to build the book.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

try:
    import duckdb
except ImportError as exc:
    raise SystemExit("Missing dependency: install duckdb with `python -m pip install duckdb`.") from exc


BUY = 66   # ASCII B
SELL = 83  # ASCII S
ADD = 65   # ASCII A
DELETE = 68  # ASCII D


def parquet_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def display_time(value: int) -> str:
    text = f"{value:09d}"
    return f"{text[:2]}:{text[2:4]}:{text[4:6]}.{text[6:]}"


def in_continuous_session(value: int) -> bool:
    return 93_000_000 <= value <= 113_000_000 or 130_000_000 <= value <= 150_000_000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=Path("sse50_20260923"))
    parser.add_argument("--date", default="20260923")
    parser.add_argument("--output", type=Path, default=Path("reconstructed_orderbook_20260923.parquet"))
    parser.add_argument("--validation", type=Path, default=Path("reconstruction_validation_20260923.csv"))
    args = parser.parse_args()

    order_file = args.input_dir / f"ord_{args.date}.parquet"
    execution_file = args.input_dir / f"exe_{args.date}.parquet"
    snapshot_file = args.input_dir / f"snp_{args.date}.parquet"
    for path in (order_file, execution_file, snapshot_file):
        if not path.is_file():
            raise SystemExit(f"Input file not found: {path}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.validation.parent.mkdir(parents=True, exist_ok=True)
    staging = args.output.with_suffix(".staging.csv")

    con = duckdb.connect()
    order_sql = parquet_path(order_file)
    execution_sql = parquet_path(execution_file)
    snapshot_sql = parquet_path(snapshot_file)
    symbols = [row[0] for row in con.execute(
        f"SELECT DISTINCT Symbol FROM read_parquet('{snapshot_sql}') ORDER BY Symbol"
    ).fetchall()]

    level_columns = []
    for level in range(1, 11):
        for side in ("Bid", "Ask"):
            level_columns.extend((f"{side}Price{level}", f"{side}Volume{level}"))
    output_columns = [
        "Symbol", "Time", "TimeText", "ProcessedEvents", "ActiveOrders",
        *level_columns, "MatchedPriceLevels", "MatchedFullLevels", "L1PriceMatch", "L1FullMatch",
    ]
    source_columns = ",".join(
        f"BidPrice{i},BidVolume{i},AskPrice{i},AskVolume{i}" for i in range(1, 11)
    )
    validation_rows = []

    with staging.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(output_columns)

        for symbol_index, symbol in enumerate(symbols, 1):
            event_query = f"""
                SELECT Time, BizIndex, 0 AS EventType, Channel, FunctionCode,
                       OrderKind, OrderOriNo, Price, Volume, 0 AS BidOrder, 0 AS AskOrder,
                       0 AS BSFlag
                FROM read_parquet('{order_sql}')
                WHERE Symbol = {symbol} AND OrderKind IN ({ADD}, {DELETE})
                UNION ALL
                SELECT Time, BizIndex, 1, Channel, 0, 0, 0, Price, Volume, BidOrder, AskOrder, BSFlag
                FROM read_parquet('{execution_sql}')
                WHERE Symbol = {symbol}
                ORDER BY Time, BizIndex, EventType
            """
            events = con.execute(event_query).fetchall()
            snapshots = con.execute(
                f"SELECT Time,{source_columns} FROM read_parquet('{snapshot_sql}') "
                f"WHERE Symbol={symbol} ORDER BY Time"
            ).fetchall()

            orders: dict[tuple[int, int], tuple[int, int, int]] = {}
            bids: dict[int, int] = defaultdict(int)
            asks: dict[int, int] = defaultdict(int)

            def adjust(side: int, price: int, delta: int) -> None:
                book = bids if side == BUY else asks
                book[price] += delta
                if book[price] <= 0:
                    book.pop(price, None)

            def reduce_order(key: tuple[int, int], volume: int) -> None:
                current = orders.get(key)
                if current is None:
                    return
                side, price, remaining = current
                reduction = min(remaining, volume)
                adjust(side, price, -reduction)
                remaining -= reduction
                if remaining:
                    orders[key] = (side, price, remaining)
                else:
                    orders.pop(key, None)

            def apply_event(event: tuple) -> None:
                _, _, event_type, channel, function_code, order_kind, order_id, price, volume, bid_id, ask_id, bs_flag = event
                if event_type == 0:
                    key = (channel, order_id)
                    if order_kind == ADD:
                        price_ticks = round(price * 100)
                        orders[key] = (function_code, price_ticks, volume)
                        adjust(function_code, price_ticks, volume)
                    else:
                        reduce_order(key, volume)
                elif bs_flag == BUY:
                    # In continuous trading, only the passive order is guaranteed
                    # to be present in the SSE order stream.
                    reduce_order((channel, ask_id), volume)
                elif bs_flag == SELL:
                    reduce_order((channel, bid_id), volume)
                else:
                    # Opening/closing auction executions have no aggressor side.
                    reduce_order((channel, bid_id), volume)
                    reduce_order((channel, ask_id), volume)

            event_index = 0
            checked = l1_price_hits = l1_full_hits = 0
            price_level_hits = full_level_hits = 0
            for snapshot in snapshots:
                snapshot_time = snapshot[0]
                while event_index < len(events) and events[event_index][0] <= snapshot_time:
                    apply_event(events[event_index])
                    event_index += 1

                bid_prices = sorted(bids, reverse=True)[:10]
                ask_prices = sorted(asks)[:10]
                rebuilt = []
                matched_prices = matched_full = 0
                source = snapshot[1:]
                for level in range(10):
                    bid_price = bid_prices[level] if level < len(bid_prices) else 0
                    ask_price = ask_prices[level] if level < len(ask_prices) else 0
                    bid_volume = bids.get(bid_price, 0)
                    ask_volume = asks.get(ask_price, 0)
                    rebuilt.extend((bid_price / 100, bid_volume, ask_price / 100, ask_volume))
                    source_bid_price = round(source[level * 4] * 100)
                    source_bid_volume = round(source[level * 4 + 1])
                    source_ask_price = round(source[level * 4 + 2] * 100)
                    source_ask_volume = round(source[level * 4 + 3])
                    price_match = bid_price == source_bid_price and ask_price == source_ask_price
                    full_match = price_match and bid_volume == source_bid_volume and ask_volume == source_ask_volume
                    matched_prices += int(price_match)
                    matched_full += int(full_match)

                l1_price_match = matched_prices >= 1 and (
                    round(rebuilt[0] * 100) == round(source[0] * 100)
                    and round(rebuilt[2] * 100) == round(source[2] * 100)
                )
                l1_full_match = l1_price_match and rebuilt[1] == round(source[1]) and rebuilt[3] == round(source[3])
                writer.writerow([
                    f"{symbol:06d}", snapshot_time, display_time(snapshot_time), event_index, len(orders),
                    *rebuilt, matched_prices, matched_full, int(l1_price_match), int(l1_full_match),
                ])

                if in_continuous_session(snapshot_time):
                    checked += 1
                    l1_price_hits += int(l1_price_match)
                    l1_full_hits += int(l1_full_match)
                    price_level_hits += matched_prices
                    full_level_hits += matched_full

            validation_rows.append([
                f"{symbol:06d}", len(events), len(snapshots), checked,
                l1_price_hits / checked if checked else 0,
                l1_full_hits / checked if checked else 0,
                price_level_hits / (checked * 10) if checked else 0,
                full_level_hits / (checked * 10) if checked else 0,
            ])
            print(f"[{symbol_index:02d}/{len(symbols)}] {symbol}: {len(events):,} events, {len(snapshots):,} snapshots")

    staging_sql = str(staging.resolve()).replace("'", "''")
    output_sql = str(args.output.resolve()).replace("'", "''")
    con.execute(f"""
        COPY (
            SELECT lpad(CAST(Symbol AS VARCHAR), 6, '0') AS Symbol, * EXCLUDE (Symbol)
            FROM read_csv_auto('{staging_sql}', header=true)
        ) TO '{output_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    staging.unlink()

    with args.validation.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "Symbol", "EventCount", "SnapshotCount", "CheckedSnapshots",
            "L1PriceMatchRate", "L1FullMatchRate", "AllLevelPriceMatchRate", "AllLevelFullMatchRate",
        ])
        writer.writerows(validation_rows)

    print(f"Output: {args.output.resolve()}")
    print(f"Validation: {args.validation.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
