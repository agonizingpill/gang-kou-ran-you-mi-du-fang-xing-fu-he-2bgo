import os,time,json
from pathlib import Path
from redis import Redis
from sqlalchemy import select
from .database import Session
from .models import FuelSample
from .round_service import evaluate_sample,audit
r=Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
# density-check：常规比对/中断恢复，force=False，已有留一判定不重算、不新增结论
# density-recompute：化验页显式重算，仍只读取封存测值，不改变封存集
QUEUES=(('density-check',False),('density-recompute',True))
while True:
    Path('/artifacts/bunker-heartbeat').write_text(str(time.time()))
    item=None
    for key,force in QUEUES:
        item=r.blpop(key,timeout=1)
        if item:
            queue_key,force=key,force;break
    if not item:continue
    id=int(item[1])
    try:
        result=evaluate_sample(id,force=force)
        with Session.begin() as s:
            x=s.get(FuelSample,id)
            if x:
                Path(f'/artifacts/sample-{id}-evidence.json').write_text(x.evidence)
                audit(s,None,'round_recomputed' if force else 'sample_compared',
                      f'{x.code} R{result["round_seq"]} {result["verdict"]} 留一罐数{len(result["folds"])}')
    except Exception as e:
        with Session.begin() as s:
            x=s.scalar(select(FuelSample).where(FuelSample.id==id).with_for_update())
            if x:x.status='failed';x.evidence=str(e)
