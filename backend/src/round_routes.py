from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from redis import Redis
import os
from .database import Session
from .models import FuelSample,Tank
from .security import current,allow
from .transfer_service import get,obj,sample_view
from .round_schemas import RoundOpenIn,ReferenceSampleIn,RecomputeIn
from . import round_service as rs
router=APIRouter()
r=Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)

@router.get('/compare-rounds')
def rounds(u=Depends(current)):
    with Session() as s:return rs.list_rounds(s)

@router.post('/compare-rounds')
def open_round(d:RoundOpenIn,u=Depends(current)):
    """主管补登参考后另开一轮；旧轮自动closed。"""
    allow(u,'admin')
    with Session.begin() as s:
        rnd=rs.open_round(s,d.grade,u,d.note)
        # 新轮开立即封存当前全部合格参考罐，供后续受理复用
        rs.seal_references(s,rnd,d.grade,u)
        return rs.round_view(s,rnd)

@router.post('/reference-samples')
def add_reference(d:ReferenceSampleIn,u=Depends(current)):
    """主管补登参考样；只写入参考库，不影响当前已封存轮次，须另开新一轮才生效。"""
    allow(u,'admin')
    with Session.begin() as s:
        tank=get(s,Tank,d.tank_id)
        x=rs.add_reference(s,d,u)
        return {**obj(x),'tank_code':tank.code,'grade':tank.grade}

@router.get('/reference-samples')
def list_references(u=Depends(current)):
    with Session() as s:
        rows=s.scalars(select(FuelSample).join(Tank,FuelSample.tank_id==Tank.id)
                       .where(FuelSample.reference==True).order_by(Tank.grade,Tank.code,FuelSample.code)).all()
        return [{**obj(x),'tank_code':s.get(Tank,x.tank_id).code,'grade':s.get(Tank,x.tank_id).grade} for x in rows]

@router.get('/samples/{id}/folds')
def sample_folds(id:int,u=Depends(current)):
    with Session() as s:
        x=get(s,FuelSample,id)
        return {'sample':sample_view(s,x),'folds':rs.fold_view(s,id)}

@router.post('/samples/{id}/recompute')
def recompute(id:int,d:RecomputeIn,u=Depends(current)):
    """化验页重算：force=False只做中断恢复（重试不新增判定）；
    force=True用封存测值重新逐罐留一，封存参考集不变。"""
    allow(u,'admin','chemist')
    with Session.begin() as s:
        x=get(s,FuelSample,id,True)
        if x.reference:raise HTTPException(409,'参考样不参与留一比对')
        tank=get(s,Tank,x.tank_id)
        rnd=rs.ensure_round(s,tank.grade,u)
        rs.seal_references(s,rnd,tank.grade,u)
        x.status='queued'
        rs.audit(s,u,'round_recompute_queued' if d.force else 'round_recover_queued',
                 f'{x.code} R{rnd.seq} force={d.force}')
    r.rpush('density-recompute' if d.force else 'density-check',id)
    return {'status':'queued','force':d.force}
