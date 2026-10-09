#!/usr/bin/env python3
"""Single-day exploratory factor search with stock/time stability diagnostics."""
from __future__ import annotations
import argparse, itertools
from pathlib import Path
import duckdb
import numpy as np
import pandas as pd

FACTORS = ["imb1", "imb5", "imb10", "micro", "momentum", "book_change", "spread", "liquidity"]

def clock_ms(s):
    x=s.astype(str).str.zfill(9)
    return ((x.str[:2].astype(int)*60+x.str[2:4].astype(int))*60+x.str[4:6].astype(int))*1000+x.str[6:].astype(int)

def prepare(path,horizon):
    p=str(path.resolve()).replace("'","''");d=duckdb.connect().execute(f"select * from read_parquet('{p}')").df();d["clock_ms"]=clock_ms(d.Time)
    d["mid"]=(d.BidPrice1+d.AskPrice1)/2;d=d[(d.mid>0)&(d.AskPrice1>d.BidPrice1)&(d.BidVolume1>0)&(d.AskVolume1>0)].sort_values(["Symbol","Time"]).copy()
    def imb(n):
        b=sum(d[f"BidVolume{i}"]/i for i in range(1,n+1));a=sum(d[f"AskVolume{i}"]/i for i in range(1,n+1));return (b-a)/(b+a).replace(0,np.nan)
    d["imb1"],d["imb5"],d["imb10"]=imb(1),imb(5),imb(10)
    d["micro"]=((d.AskPrice1*d.BidVolume1+d.BidPrice1*d.AskVolume1)/(d.BidVolume1+d.AskVolume1)-d.mid)/(d.AskPrice1-d.BidPrice1)
    g=d.groupby("Symbol",group_keys=False);d["momentum"]=g.mid.pct_change(20);d["book_change"]=g.imb5.diff(20)
    d["spread"]=(d.AskPrice1-d.BidPrice1)/d.mid;d["liquidity"]=np.log1p(d.BidVolume1+d.AskVolume1)
    future=d[["Symbol","clock_ms","mid"]].rename(columns={"clock_ms":"future_time","mid":"future_mid"})
    left=d[["Symbol","Time","clock_ms","mid",*FACTORS]].copy();left["target_time"]=left.clock_ms+horizon*1000
    x=pd.merge_asof(left.sort_values("target_time"),future.sort_values("future_time"),left_on="target_time",right_on="future_time",by="Symbol",direction="forward",tolerance=10_000)
    x["target"]=x.future_mid/x.mid-1;x=x.replace([np.inf,-np.inf],np.nan).dropna(subset=FACTORS+["target"])
    x["regime"]=np.select([x.Time<100_000_000,x.Time<113_000_000,x.Time<143_000_000],["open","morning","afternoon"],default="close")
    x["time_block"]=x.clock_ms//1_800_000
    return x

def corr_by(d,key):
    def one(z):
        if z.score.nunique() < 2 or z.target.nunique() < 2:
            return np.nan
        return z.score.rank().corr(z.target.rank())
    return d.groupby(key,observed=True).apply(one,include_groups=False).dropna()

def bootstrap(values,n,rng):
    a=np.asarray(values,float);draw=a[rng.integers(0,len(a),(n,len(a)))].mean(axis=1)
    return np.mean(a),np.quantile(draw,.025),np.quantile(draw,.975)

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--book",type=Path,default=Path("reconstructed_orderbook_20260923.parquet"));ap.add_argument("--horizon-seconds",type=int,default=60);ap.add_argument("--max-factors",type=int,default=4);ap.add_argument("--bootstrap",type=int,default=2000);ap.add_argument("--output-dir",type=Path,default=Path("daily_factor_output"));a=ap.parse_args();a.output_dir.mkdir(parents=True,exist_ok=True)
    d=prepare(a.book,a.horizon_seconds);mu=d[FACTORS].mean();sd=d[FACTORS].std().replace(0,1);z=(d[FACTORS]-mu)/sd;y=d.target.to_numpy();rows=[];scores={}
    for size in range(1,a.max_factors+1):
        for cols in itertools.combinations(FACTORS,size):
            X=z[list(cols)].to_numpy();coef=np.linalg.lstsq(np.c_[np.ones(len(X)),X],y,rcond=None)[0];score=np.c_[np.ones(len(X)),X]@coef
            q=d[["Symbol","regime","time_block","target"]].copy();q["score"]=score
            by_symbol=corr_by(q,"Symbol");by_regime=corr_by(q,"regime");by_block=corr_by(q,"time_block")
            name="+".join(cols);scores[name]=(score,coef)
            stable=(by_regime.min()>0) and ((by_symbol>0).mean()>=.7)
            rows.append((name,size,by_symbol.mean(),by_symbol.median(),(by_symbol>0).mean(),by_regime.mean(),by_regime.min(),by_regime.std(),by_block.mean(),(by_block>0).mean(),stable))
    rank=pd.DataFrame(rows,columns=["Factors","FactorCount","MeanStockIC","MedianStockIC","PositiveStockRatio","MeanRegimeIC","MinRegimeIC","RegimeICStd","MeanTimeBlockIC","PositiveTimeBlockRatio","Stable"])
    rank=rank.sort_values(["Stable","MeanStockIC","PositiveStockRatio"],ascending=False).reset_index(drop=True);rank.insert(0,"Rank",np.arange(1,len(rank)+1));rank.to_csv(a.output_dir/f"combination_ranking_{a.horizon_seconds}s.csv",index=False,encoding="utf-8-sig")
    winner=rank.iloc[0].Factors;d["score"]=scores[winner][0]
    symbol=corr_by(d,"Symbol").rename("IC").reset_index();symbol.to_csv(a.output_dir/f"winner_by_symbol_{a.horizon_seconds}s.csv",index=False,encoding="utf-8-sig")
    regime=corr_by(d,"regime").rename("IC").reset_index();regime.to_csv(a.output_dir/f"winner_by_regime_{a.horizon_seconds}s.csv",index=False,encoding="utf-8-sig")
    block=corr_by(d,"time_block").rename("IC").reset_index();block.to_csv(a.output_dir/f"winner_by_time_block_{a.horizon_seconds}s.csv",index=False,encoding="utf-8-sig")
    rng=np.random.default_rng(20260923);sm=bootstrap(symbol.IC,a.bootstrap,rng);tm=bootstrap(block.IC,a.bootstrap,rng)
    coefficient_rows=[(f"Coefficient_{name}",value) for name,value in zip(["Intercept",*winner.split("+")],scores[winner][1])]
    summary=pd.DataFrame([("SelectionScope","2026-09-23 in-sample only"),("HorizonSeconds",a.horizon_seconds),("Winner",winner),("CombinationsTested",len(rank)),*coefficient_rows,("MeanStockIC",sm[0]),("StockBootstrapCI95Low",sm[1]),("StockBootstrapCI95High",sm[2]),("PositiveStockRatio",float((symbol.IC>0).mean())),("MeanTimeBlockIC",tm[0]),("TimeBlockBootstrapCI95Low",tm[1]),("TimeBlockBootstrapCI95High",tm[2]),("PositiveTimeBlockRatio",float((block.IC>0).mean())),("MinRegimeIC",float(regime.IC.min()))],columns=["Metric","Value"])
    summary.to_csv(a.output_dir/f"summary_{a.horizon_seconds}s.csv",index=False,encoding="utf-8-sig")
    print(rank.head(10).to_string(index=False));print("\n",summary.to_string(index=False));print("\nBy regime:\n",regime.to_string(index=False))

if __name__=="__main__":main()
