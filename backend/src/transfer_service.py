from datetime import datetime,timezone
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import select
from .models import Tank,FuelSample,Transfer,Audit
from .round_models import CompareRound,SampleFoldResult
from .density_model import within_spec

def obj(x):return {c.name:float(getattr(x,c.name))if isinstance(getattr(x,c.name),Decimal)else getattr(x,c.name)for c in x.__table__.columns}
def get(s,model,id,lock=False):
    q=select(model).where(model.id==id)
    if lock:q=q.with_for_update().execution_options(populate_existing=True)
    x=s.scalar(q)
    if not x:raise HTTPException(404,'记录不存在')
    return x
def audit(s,u,action,detail):s.add(Audit(actor=u['email'],action=action,detail=str(detail),at=datetime.now(timezone.utc).isoformat()))
def sample_view(s,x):return {**obj(x),'tank_code':s.get(Tank,x.tank_id).code,'grade':s.get(Tank,x.tank_id).grade}
def transfer_view(s,x):
    sample=s.get(FuelSample,x.sample_id);tank=s.get(Tank,sample.tank_id)
    return {**obj(x),'sample_code':sample.code,'tank_code':tank.code,'tank_liters':float(tank.liters)}
def current_round_basis(s,grade):
    rnd=s.scalar(select(CompareRound).where(CompareRound.grade==grade,CompareRound.state=='active').order_by(CompareRound.seq.desc()))
    return rnd
def all_folds_pass_current_round(s,x):
    """未签调拨只认当前轮：当前轮必须存在逐罐留一、全部passed，
    且留一打分密度与当前有效密度（复检后为复检密度）一致。
    返回 True=全部过线；False=存在未过线/参考不足/翻转/依据过期；None=当前轮无该样留一结果。"""
    t=get(s,Tank,x.tank_id);rnd=current_round_basis(s,t.grade)
    if not rnd:return None
    folds=s.scalars(select(SampleFoldResult).where(SampleFoldResult.sample_id==x.id,SampleFoldResult.round_id==rnd.id)).all()
    if not folds:return None
    effective=float(x.retest_density) if x.retest_density is not None else float(x.density_15)
    import json
    for f in folds:
        try:
            if abs(float(json.loads(f.evidence).get('score_density',effective))-effective)>1e-9:return False
        except (ValueError,TypeError):pass
    return True if all(f.fold_state=='passed' for f in folds) else False
def eligible(s,x):
    if x.reference:raise HTTPException(409,'参考样不能用于实船调拨')
    if x.status=='review':raise HTTPException(409,'留一比对存在参考不足或结论翻转，该油样待复核，不能申请调拨')
    if x.status not in ['qualified','retested']:raise HTTPException(409,'油样未合格或异常尚未通过复检，不能申请调拨')
    t=get(s,Tank,x.tank_id)
    density=x.retest_density if x.retest_density is not None else x.density_15
    if not within_spec(t.grade,density,x.viscosity,x.sulfur):raise HTTPException(409,'复检或原样仍超出本等级密度/黏度/硫规格')
    basis=all_folds_pass_current_round(s,x)
    if basis is None:raise HTTPException(409,'当前比对轮缺少该油样的逐罐留一结果，请先在化验页完成比对')
    if basis is False:
        if x.retest_density is not None:raise HTTPException(409,'复检密度与封存留一打分密度不一致，请在化验页按复检密度重新逐罐留一且全部过线后再申请')
        raise HTTPException(409,'当前轮逐罐留一未全部过线（参考不足或结论翻转），不能申请调拨')
    rnd=current_round_basis(s,t.grade)
    return t,density,rnd

def retest(s,x,d,u):
    if x.reference or x.status not in ['anomaly','rejected','retested','review']:raise HTTPException(409,'仅异常待复检或未通过样可复检')
    if s.scalar(select(Transfer.id).where(Transfer.sample_id==x.id).limit(1)):raise HTTPException(409,'油样已被调拨引用，化验快照冻结')
    t=get(s,Tank,x.tank_id);x.retest_density=d.density;x.retest_note=d.note
    x.status='retested'if within_spec(t.grade,d.density,x.viscosity,x.sulfur)else'rejected';audit(s,u,'sample_retested',f'{x.code}:{x.status}');return sample_view(s,x)
def request_transfer(s,x,d,u):
    t,density,rnd=eligible(s,x)
    if t.status!='active':raise HTTPException(409,'储罐处于暂停调拨状态')
    if d.liters>t.liters:raise HTTPException(409,'申请调拨量超过罐内可用量，未扣减')
    tr=Transfer(**d.model_dump(),status='requested',grade_snapshot=t.grade,density_snapshot=density,viscosity_snapshot=x.viscosity,sulfur_snapshot=x.sulfur,released_by='',basis_round_id=rnd.id,basis_round_seq=rnd.seq);s.add(tr);s.flush();audit(s,u,'transfer_requested',f'{tr.code}:依据R{rnd.seq}');return transfer_view(s,tr)
def release(s,tr,u):
    if tr.status!='requested':raise HTTPException(409,'调拨单已放行，不能重复扣减')
    x=get(s,FuelSample,tr.sample_id);t=get(s,Tank,x.tank_id,True)
    if t.status!='active':raise HTTPException(409,'储罐暂停中，不能放行')
    if not within_spec(tr.grade_snapshot,tr.density_snapshot,tr.viscosity_snapshot,tr.sulfur_snapshot):raise HTTPException(409,'调拨化验快照不满足等级规格')
    # 未签调拨只认当前轮：换轮后旧申请若不再有当前轮全部过线依据则拦截
    current=current_round_basis(s,tr.grade_snapshot)
    if current and tr.basis_round_id!=current.id:
        if all_folds_pass_current_round(s,x) is not True:raise HTTPException(409,'比对轮已切换，该未签调拨的当前轮留一依据不足，请重新比对后再申请')
        tr.basis_round_id=current.id;tr.basis_round_seq=current.seq
    if tr.liters>t.liters:raise HTTPException(409,'罐内可用量不足，调拨未扣减')
    from .harbor_service import occupy_for_release
    occupy_for_release(s,tr,u)
    t.liters-=tr.liters;tr.status='released';tr.released_by=u['email'];audit(s,u,'transfer_released',f'{tr.code}:{tr.liters}L 依据R{tr.basis_round_seq}');s.flush();return transfer_view(s,tr)
