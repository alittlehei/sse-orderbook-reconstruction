#!/usr/bin/env python3
"""Out-of-sample order-book factor experiment on reconstructed snapshots."""
from __future__ import annotations
import argparse, csv
from pathlib import Path
import numpy as np
import pandas as pd
import duckdb

FACTORS = ["imb1", "imb5", "imb10", "micro", "momentum", "book_change", "spread", "liquidity"]

def clock_ms(series):
    text = series.astype(str).str.zfill(9)
    return ((text.str[:2].astype(int) * 60 + text.str[2:4].astype(int)) * 60 +
            text.str[4:6].astype(int)) * 1000 + text.str[6:].astype(int)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=Path, default=Path("reconstructed_orderbook_20260923.parquet"))
    ap.add_argument("--horizon-seconds", type=int, default=60)
    ap.add_argument("--out", type=Path, default=Path("factor_research_20260923.csv"))
    args = ap.parse_args()
    book = str(args.book.resolve()).replace("'", "''")
    df = duckdb.connect().execute(f"SELECT * FROM read_parquet('{book}')").df()
    df["clock_ms"] = clock_ms(df.Time)
    df["mid"] = (df.BidPrice1 + df.AskPrice1) / 2
    good = (df.mid > 0) & (df.AskPrice1 > df.BidPrice1) & (df.BidVolume1 > 0) & (df.AskVolume1 > 0)
    df = df.loc[good].sort_values(["Symbol", "Time"]).copy()
    def imbalance(n):
        b = sum(df[f"BidVolume{i}"] / i for i in range(1, n + 1))
        a = sum(df[f"AskVolume{i}"] / i for i in range(1, n + 1))
        return (b - a) / (b + a).replace(0, np.nan)
    df["imb1"] = imbalance(1)
    df["imb5"] = imbalance(5)
    df["imb10"] = imbalance(10)
    df["micro"] = ((df.AskPrice1 * df.BidVolume1 + df.BidPrice1 * df.AskVolume1) /
                    (df.BidVolume1 + df.AskVolume1) - df.mid) / (df.AskPrice1 - df.BidPrice1)
    g = df.groupby("Symbol", group_keys=False)
    df["momentum"] = g["mid"].pct_change(20)
    df["book_change"] = g["imb5"].diff(20)
    df["spread"] = (df.AskPrice1 - df.BidPrice1) / df.mid
    df["liquidity"] = np.log1p(df.BidVolume1 + df.AskVolume1)
    # Align to the first observation at or after the requested future horizon.
    future = df[["Symbol", "clock_ms", "mid"]].rename(columns={"clock_ms": "future_time", "mid": "future_mid"})
    left = df[["Symbol", "Time", "clock_ms", "mid", *FACTORS]].copy()
    left["target_time"] = left.clock_ms + args.horizon_seconds * 1000
    merged = pd.merge_asof(left.sort_values("target_time"), future.sort_values("future_time"),
                           left_on="target_time", right_on="future_time", by="Symbol", direction="forward",
                           tolerance=10_000)
    merged["target"] = merged.future_mid / merged.mid - 1
    merged = merged.replace([np.inf, -np.inf], np.nan).dropna(subset=FACTORS + ["target"])
    # Use time blocks: model selection only on validation, final number is test.
    t = merged.Time
    train = merged[t < 120000000]
    valid = merged[(t >= 120000000) & (t < 140000000)]
    test = merged[t >= 140000000]
    combos = {
        "imb1": ["imb1"], "imb5": ["imb5"], "imb10": ["imb10"],
        "micro": ["micro"], "momentum": ["momentum"], "book_change": ["book_change"],
        "micro_imb5": ["micro", "imb5"], "flow_price": ["micro", "imb5", "momentum"],
        "full": FACTORS,
    }
    rows = []
    for name, cols in combos.items():
        # Cross-sectional z-score + OLS fit on the training block.
        mu, sd = train[cols].mean(), train[cols].std().replace(0, 1)
        xtr = ((train[cols] - mu) / sd).to_numpy(); ytr = train.target.to_numpy()
        coef = np.linalg.lstsq(np.c_[np.ones(len(xtr)), xtr], ytr, rcond=None)[0]
        for split, part in (("train", train), ("validation", valid), ("test", test)):
            x = ((part[cols] - mu) / sd).to_numpy()
            score = np.c_[np.ones(len(x)), x] @ coef
            tmp = part[["Symbol", "Time", "target"]].copy(); tmp["score"] = score
            ic = tmp[["score", "target"]].corr(method="spearman").iloc[0, 1]
            rank = tmp.groupby("Time")["score"].rank(pct=True)
            top = tmp.target.where(rank >= 0.9).groupby(tmp.Time).mean()
            bottom = tmp.target.where(rank <= 0.1).groupby(tmp.Time).mean()
            q = top - bottom
            rows.append((name, "+".join(cols), split, len(part), ic, q.mean(), q.median(), float(np.std(q))))
    out = pd.DataFrame(rows, columns=["Combo", "Factors", "Split", "Observations", "SpearmanIC", "TopBottomSpreadMean", "TopBottomSpreadMedian", "TopBottomSpreadStd"])
    args.out.parent.mkdir(parents=True, exist_ok=True); out.to_csv(args.out, index=False, encoding="utf-8-sig")
    val = out[out.Split == "validation"].sort_values("SpearmanIC", ascending=False)
    print(val[["Combo", "SpearmanIC", "TopBottomSpreadMean"]].to_string(index=False))
    print("Selected by validation IC:", val.iloc[0].Combo)
    print("Test result:")
    print(out[(out.Split == "test") & (out.Combo == val.iloc[0].Combo)].to_string(index=False))
    print("Output:", args.out.resolve())

if __name__ == "__main__": main()
