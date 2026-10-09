#!/usr/bin/env python3
"""Grouped out-of-fold factor research with complete trading days per symbol."""
from __future__ import annotations
import argparse
from pathlib import Path
import duckdb
import numpy as np
import pandas as pd

FACTORS = ["imb1", "imb5", "imb10", "micro", "momentum", "book_change", "spread", "liquidity"]
COMBOS = {
    "imb1": ["imb1"], "imb5": ["imb5"], "imb10": ["imb10"], "micro": ["micro"],
    "momentum": ["momentum"], "book_change": ["book_change"],
    "micro_imb5": ["micro", "imb5"],
    "flow_price": ["micro", "imb5", "momentum"],
    "full": FACTORS,
}

def clock_ms(series):
    s = series.astype(str).str.zfill(9)
    return ((s.str[:2].astype(int) * 60 + s.str[2:4].astype(int)) * 60 +
            s.str[4:6].astype(int)) * 1000 + s.str[6:].astype(int)

def prepare(path: Path, horizon: int):
    p = str(path.resolve()).replace("'", "''")
    d = duckdb.connect().execute(f"SELECT * FROM read_parquet('{p}')").df()
    d["clock_ms"] = clock_ms(d.Time)
    d["mid"] = (d.BidPrice1 + d.AskPrice1) / 2
    d = d[(d.mid > 0) & (d.AskPrice1 > d.BidPrice1) & (d.BidVolume1 > 0) & (d.AskVolume1 > 0)].sort_values(["Symbol", "Time"]).copy()
    def imb(n):
        b = sum(d[f"BidVolume{i}"] / i for i in range(1, n + 1)); a = sum(d[f"AskVolume{i}"] / i for i in range(1, n + 1))
        return (b - a) / (b + a).replace(0, np.nan)
    d["imb1"], d["imb5"], d["imb10"] = imb(1), imb(5), imb(10)
    d["micro"] = ((d.AskPrice1*d.BidVolume1+d.BidPrice1*d.AskVolume1)/(d.BidVolume1+d.AskVolume1)-d.mid)/(d.AskPrice1-d.BidPrice1)
    g = d.groupby("Symbol", group_keys=False)
    d["momentum"] = g.mid.pct_change(20); d["book_change"] = g.imb5.diff(20)
    d["spread"] = (d.AskPrice1-d.BidPrice1)/d.mid; d["liquidity"] = np.log1p(d.BidVolume1+d.AskVolume1)
    future = d[["Symbol","clock_ms","mid"]].rename(columns={"clock_ms":"future_time","mid":"future_mid"})
    left = d[["Symbol","Time","clock_ms","mid",*FACTORS]].copy(); left["target_time"] = left.clock_ms+horizon*1000
    x = pd.merge_asof(left.sort_values("target_time"),future.sort_values("future_time"),left_on="target_time",right_on="future_time",by="Symbol",direction="forward",tolerance=10_000)
    x["target"] = x.future_mid/x.mid-1
    return x.replace([np.inf,-np.inf],np.nan).dropna(subset=FACTORS+["target"])

def regime(t):
    return np.select([t < 100_000_000, t < 113_000_000, t < 143_000_000], ["open_0930_1000","morning_1000_1130","afternoon_1300_1430"], default="close_1430_1500")

def metrics(part):
    part = part.copy()
    part["bucket"] = part.clock_ms // 3000
    counts = part.groupby("bucket").Symbol.transform("nunique")
    part = part[counts >= 5]
    ranks = part.groupby("bucket").score.rank(pct=True)
    target_ranks = part.groupby("bucket").target.rank(pct=True)
    by_time_ic = ranks.groupby(part.bucket).corr(target_ranks).dropna()
    top = part.target.where(ranks >= .8).groupby(part.bucket).mean()
    bottom = part.target.where(ranks <= .2).groupby(part.bucket).mean()
    spread = top-bottom
    return len(part), by_time_ic.mean(), spread.mean(), spread.median(), len(by_time_ic)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--book",type=Path,default=Path("reconstructed_orderbook_20260923.parquet")); ap.add_argument("--horizon-seconds",type=int,default=60); ap.add_argument("--folds",type=int,default=5); ap.add_argument("--out",type=Path,default=Path("grouped_factor_research.csv")); a=ap.parse_args()
    d=prepare(a.book,a.horizon_seconds); symbols=np.array(sorted(d.Symbol.unique())); rng=np.random.default_rng(20260923); rng.shuffle(symbols); folds=np.array_split(symbols,a.folds)
    output=[]
    for name,cols in COMBOS.items():
        predictions=[]
        for fold,test_symbols in enumerate(folds):
            train=d[~d.Symbol.isin(test_symbols)]; test=d[d.Symbol.isin(test_symbols)].copy()
            mu=train[cols].mean(); sd=train[cols].std().replace(0,1)
            x=((train[cols]-mu)/sd).to_numpy(); coef=np.linalg.lstsq(np.c_[np.ones(len(x)),x],train.target.to_numpy(),rcond=None)[0]
            xt=((test[cols]-mu)/sd).to_numpy(); test["score"]=np.c_[np.ones(len(xt)),xt]@coef; test["fold"]=fold; predictions.append(test)
        oof=pd.concat(predictions,ignore_index=True); oof["regime"]=regime(oof.Time)
        vals=[]
        for label,part in [("all",oof),*list(oof.groupby("regime"))]:
            m=metrics(part); output.append((a.horizon_seconds,name,"+".join(cols),label,*m));
            if label!="all": vals.append(m[1])
        robust=float(np.mean(vals)-np.std(vals)-max(0,-min(vals)))
        output.append((a.horizon_seconds,name,"+".join(cols),"robust_score",len(oof),robust,np.nan,np.nan,len(vals)))
    columns=["HorizonSeconds","Combo","Factors","Regime","Observations","MeanCrossSectionalIC","TopBottomSpreadMean","TopBottomSpreadMedian","TimeSlices"]
    out=pd.DataFrame(output,columns=columns); a.out.parent.mkdir(parents=True,exist_ok=True);out.to_csv(a.out,index=False,encoding="utf-8-sig")
    ranking=out[out.Regime=="robust_score"].sort_values("MeanCrossSectionalIC",ascending=False)
    print(ranking[["Combo","MeanCrossSectionalIC"]].to_string(index=False)); winner=ranking.iloc[0].Combo
    print("Selected:",winner);print(out[(out.Combo==winner)&(out.Regime!="robust_score")][["Regime","MeanCrossSectionalIC","TopBottomSpreadMean"]].to_string(index=False))

if __name__=="__main__": main()
