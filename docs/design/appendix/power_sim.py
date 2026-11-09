# 估算：配对 + 按场景聚类 bootstrap，在不同场景数下能检测到多大的提升
import numpy as np, sys
rng=np.random.default_rng(0)
def sim(S, delta, R=3, reps=3, base=0.7, conc=4.0, B=1000, sims=300):
    hit=0; widths=[]
    for _ in range(sims):
        a=base*conc; b=(1-base)*conc
        pA=rng.beta(a,b,size=S)                      # 每个场景的难度不同
        pB=np.clip(pA+delta,0,1)
        # 每个场景 R 条录制，每条跑 reps 次；用例得分 = reps 次的均值
        sA=rng.binomial(reps,np.repeat(pA,R))/reps
        sB=rng.binomial(reps,np.repeat(pB,R))/reps
        d=(sB-sA).reshape(S,R).mean(1)               # 场景级配对差
        idx=rng.integers(0,S,size=(B,S))
        boot=d[idx].mean(1)
        lo,hi=np.percentile(boot,[2.5,97.5])
        widths.append((hi-lo)/2); hit+= lo>0
    return hit/sims, np.mean(widths)
print("S场景  delta  检出率  CI半宽")
for S in [12,16,24,32,48]:
    for delta in [0.05,0.10,0.15]:
        p,w=sim(S,delta); print(f"{S:5d}  {delta:.2f}  {p:5.2f}  ±{w:.3f}")
