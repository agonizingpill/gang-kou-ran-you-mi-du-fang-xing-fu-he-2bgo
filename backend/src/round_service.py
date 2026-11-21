import json
from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select,func
from .models import Tank,FuelSample,Audit
from .round_models import CompareRound,RoundReference,SampleFoldResult
from .density_model import within_spec,fit_fold

def _now():return datetime.now(timezone.utc).isoformat()
def audit(s,u,action,detail):s.add(Audit(actor=u['email'] if u else 'worker',action=action,detail=str(detail),at=_now()))

def active_round(s,grade,lock=False):
    q=select(CompareRound).where(CompareRound.grade==grade,CompareRound.state=='active').order_by(CompareRound.seq.desc())
    if lock:q=q.with_for_update()
    return s.scalar(q)

def ensure_round(s,grade,u):
    """取当前轮；该等级从未开轮时自动建立第1轮（幂等）。"""
    rnd=active_round(s,grade,lock=True)
    if rnd:return rnd
    seq=1+(s.scalar(select(func.coalesce(func.max(CompareRound.seq),0)).where(CompareRound.grade==grade)) or 0)
    rnd=CompareRound(seq=seq,grade=grade,state='active',opened_by=u['email'] if u else 'system',opened_at=_now())
    s.add(rnd);s.flush();audit(s,u,'round_opened',f'{grade} R{seq}: 自动开立首轮')
    return rnd

def open_round(s,grade,u,note=''):
    """主管补登参考后另开一轮：旧轮转closed，同等级仅一轮active。"""
    current=active_round(s,grade,lock=True)
    if current:
        current.state='closed';current.closed_at=_now()
    seq=1+(s.scalar(select(func.coalesce(func.max(CompareRound.seq),0)).where(CompareRound.grade==grade)) or 0)
    rnd=CompareRound(seq=seq,grade=grade,state='active',opened_by=u['email'],opened_at=_now())
    s.add(rnd);s.flush();audit(s,u,'round_opened',f'{grade} R{seq}: 另开新一轮 {note}'.strip())
    return rnd

def seal_references(s,rnd,grade,u=None):
    """受理时封存该等级全部参考罐与测值；已封存的不重复写入（幂等）。
    返回按罐分组的封存记录 {tank_id: [RoundReference,...]}。"""
    existing={x.sample_id:x for x in s.scalars(select(RoundReference).where(RoundReference.round_id==rnd.id)).all()}
    refs=s.scalars(select(FuelSample).join(Tank,FuelSample.tank_id==Tank.id)
                   .where(FuelSample.reference==True,Tank.grade==grade).order_by(Tank.code,FuelSample.code)).all()
    added=0
    for ref in refs:
        if ref.id in existing:continue
        tank=s.get(Tank,ref.tank_id)
        s.add(RoundReference(round_id=rnd.id,sample_id=ref.id,tank_id=ref.tank_id,code=ref.code,
                             tank_code=tank.code,density_15=ref.density_15,sealed_at=_now()));added+=1
    s.flush()
    if added:audit(s,u,'round_sealed',f'{grade} R{rnd.seq}: 封存{added}条参考罐测值')
    grouped={}
    for rr in s.scalars(select(RoundReference).where(RoundReference.round_id==rnd.id).order_by(RoundReference.tank_code)).all():
        grouped.setdefault(rr.tank_id,[]).append(rr)
    return grouped

def evaluate_sample(sample_id,force=False):
    """worker调用：用封存测值逐罐留一。force=False时只补缺/恢复，不覆盖已有判定；
    force=True时按封存测值重算，但封存集本身不变。返回汇总dict。"""
    from .database import Session
    with Session.begin() as s:
        sample=s.scalar(select(FuelSample).where(FuelSample.id==sample_id).with_for_update())
        if not sample:raise ValueError('样品不存在')
        tank=s.get(Tank,sample.tank_id);grade=tank.grade
        rnd=active_round(s,grade,lock=True)
        if not rnd:raise ValueError(f'{grade}尚无比对轮次')
        grouped=seal_references(s,rnd,grade)
        if not grouped:raise ValueError(f'{grade} R{rnd.seq} 封存参考为空，无法比对')
        # 复检过的油样按复检有效密度打分；原样密度始终保留在FuelSample
        effective_density=float(sample.retest_density) if sample.retest_density is not None else float(sample.density_15)
        folds={f.held_out_tank_id:f for f in s.scalars(
            select(SampleFoldResult).where(SampleFoldResult.sample_id==sample_id,SampleFoldResult.round_id==rnd.id)).all()}
        spec_ok=within_spec(grade,sample.density_15,sample.viscosity,sample.sulfur)
        summary=[]
        for held_tank_id,held in grouped.items():
            held_codes=[x.code for x in held]
            row=folds.get(held_tank_id)
            if row is not None and not force:
                summary.append({'held_out_tank_code':row.held_out_tank_code,'fold_state':row.fold_state,
                                'fit_count':row.fit_count,'score':float(row.score) if row.score is not None else None,
                                'threshold':float(row.threshold) if row.threshold is not None else None,
                                'spec_ok':row.spec_ok,'recovered':True,'held_codes':held_codes});continue
            train=[float(x.density_15) for tid,items in grouped.items() if tid!=held_tank_id for x in items]
            if row is None:
                row=SampleFoldResult(sample_id=sample_id,round_id=rnd.id,held_out_tank_id=held_tank_id,
                                     held_out_tank_code=held[0].tank_code);s.add(row)
            row.held_out_tank_code=held[0].tank_code;row.updated_at=_now()
            try:
                score,threshold,ev=fit_fold(train,effective_density)
                ev['score_density']=effective_density
                row.fit_count=ev['fit_count'];row.score=score;row.threshold=threshold;row.spec_ok=spec_ok
                row.fold_state='passed' if (score>=threshold and spec_ok) else 'anomaly'
                ev['held_out_tank_code']=row.held_out_tank_code;ev['held_codes']=held_codes
                ev['fit_scope']=f'封存参考中剔除罐 {row.held_out_tank_code} 后同等级留一拟合'
                row.evidence=json.dumps(ev,ensure_ascii=False)
            except ValueError as e:
                row.fit_count=len(train);row.score=None;row.threshold=None;row.spec_ok=spec_ok
                row.fold_state='insufficient';row.evidence=json.dumps({'reason':str(e),'held_out_tank_code':row.held_out_tank_code,
                                                                        'held_codes':held_codes,'fit_count':len(train),
                                                                        'score_density':effective_density},ensure_ascii=False)
            summary.append({'held_out_tank_code':row.held_out_tank_code,'fold_state':row.fold_state,'fit_count':row.fit_count,
                            'score':float(row.score) if row.score is not None else None,
                            'threshold':float(row.threshold) if row.threshold is not None else None,
                            'spec_ok':spec_ok,'recovered':False,'held_codes':held_codes})
        s.flush()
        states=[x['fold_state'] for x in summary]
        # 每种留一都过线且符合规格才合格；任一组参考不足、或组间结论翻转，一律待复核
        if any(v=='insufficient' for v in states):verdict='review'
        elif all(v=='passed' for v in states):verdict='qualified'
        elif all(v=='anomaly' for v in states):verdict='anomaly'
        else:verdict='review'
        # 已人工复检合格的样品，留一通过后保留「复检合格」状态，不回写成原样合格
        if verdict=='qualified' and sample.status=='retested':verdict='retested'
        sample.status=verdict
        agg_score=next((x['score'] for x in summary if x['score'] is not None),None)
        agg_thr=next((x['threshold'] for x in summary if x['threshold'] is not None),None)
        sample.log_density=agg_score;sample.threshold=agg_thr
        sample.evidence=json.dumps({'mode':'leave_one_tank_out','grade':grade,'round_id':rnd.id,'round_seq':rnd.seq,
                                    'fold_count':len(summary),'verdict':verdict,'spec_ok':spec_ok,
                                    'sealed_reference_tanks':sorted({rr.tank_code for rr in sum(grouped.values(),[])}),
                                    'folds':summary},ensure_ascii=False)
        return {'sample_id':sample_id,'round_id':rnd.id,'round_seq':rnd.seq,'verdict':verdict,'folds':summary}

def fold_view(s,sample_id):
    rows=s.scalars(select(SampleFoldResult).join(CompareRound,SampleFoldResult.round_id==CompareRound.id)
                   .where(SampleFoldResult.sample_id==sample_id).order_by(CompareRound.seq.desc(),SampleFoldResult.id)).all()
    out=[]
    for f in rows:
        rnd=s.get(CompareRound,f.round_id)
        out.append({'round_seq':rnd.seq,'round_state':rnd.state,'held_out_tank_code':f.held_out_tank_code,
                    'fold_state':f.fold_state,'fit_count':f.fit_count,
                    'score':float(f.score) if f.score is not None else None,
                    'threshold':float(f.threshold) if f.threshold is not None else None,
                    'spec_ok':f.spec_ok,'updated_at':f.updated_at,
                    'evidence':json.loads(f.evidence) if f.evidence else {}})
    return out

def round_view(s,rnd):
    refs=s.scalars(select(RoundReference).where(RoundReference.round_id==rnd.id).order_by(RoundReference.tank_code,RoundReference.code)).all()
    tanks={}
    for rr in refs:tanks.setdefault(rr.tank_code,[]).append({'code':rr.code,'density_15':float(rr.density_15)})
    return {'id':rnd.id,'seq':rnd.seq,'grade':rnd.grade,'state':rnd.state,'opened_by':rnd.opened_by,
            'opened_at':rnd.opened_at,'closed_at':rnd.closed_at,
            'reference_count':len(refs),'tank_count':len(tanks),'tanks':tanks}

def list_rounds(s):
    out=[]
    for grade in ('VLSFO','MGO'):
        rows=s.scalars(select(CompareRound).where(CompareRound.grade==grade).order_by(CompareRound.seq.desc())).all()
        current=active_round(s,grade)
        out.append({'grade':grade,'active_round_seq':current.seq if current else None,
                    'rounds':[round_view(s,r) for r in rows]})
    return out

def add_reference(s,d,u):
    tank=s.get(Tank,d.tank_id)
    if not tank:raise HTTPException(404,'来源储罐不存在')
    if not within_spec(tank.grade,d.density_15,d.viscosity,d.sulfur):
        raise HTTPException(409,'补登参考样不符合该储罐等级的密度/黏度/硫规格')
    x=FuelSample(code=d.code,tank_id=d.tank_id,density_15=d.density_15,viscosity=d.viscosity,sulfur=d.sulfur,
                 status='qualified',reference=True,evidence=d.note,retest_note='')
    s.add(x);s.flush();audit(s,u,'reference_registered',f'{x.code}@{tank.code} {tank.grade} 密度{d.density_15}（补登后须另开新一轮生效）')
    return x
