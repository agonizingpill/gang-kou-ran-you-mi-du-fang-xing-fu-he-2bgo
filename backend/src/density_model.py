import numpy as np
from sqlalchemy import select
from sklearn.preprocessing import StandardScaler
from sklearn.neighbors import KernelDensity
from .models import FuelSample,Tank
from .constants import GRADES,MIN_REFERENCES

def within_spec(grade,density,viscosity,sulfur):
    g=GRADES[grade];return g['density_min']<=float(density)<=g['density_max'] and g['viscosity_min']<=float(viscosity)<=g['viscosity_max'] and float(sulfur)<=g['sulfur_max']
def compare(s,sample):
    tank=s.get(Tank,sample.tank_id)
    refs=s.scalars(select(FuelSample).join(Tank,FuelSample.tank_id==Tank.id).where(FuelSample.reference==True,Tank.grade==tank.grade).order_by(FuelSample.id)).all()
    values=np.array([[float(x.density_15)]for x in refs])
    if len(values)<MIN_REFERENCES:raise ValueError(f'本等级合格参考样不足{MIN_REFERENCES}条')
    scaler=StandardScaler();z=scaler.fit_transform(values)
    if float(scaler.var_[0])<1e-8:raise ValueError('参考密度没有足够变化，不能拟合核密度')
    model=KernelDensity(kernel='gaussian',bandwidth=.3).fit(z)
    scores=model.score_samples(z);threshold=float(np.quantile(scores,.05))
    score=float(model.score_samples(scaler.transform([[float(sample.density_15)]]))[0])
    passed=score>=threshold and within_spec(tank.grade,sample.density_15,sample.viscosity,sample.sulfur)
    grid=np.linspace(float(values.min()-8),float(values.max()+8),70)
    curve=np.exp(model.score_samples(scaler.transform(grid.reshape(-1,1))))/float(scaler.scale_[0])
    return score,threshold,passed,{'grade':tank.grade,'reference_count':len(refs),'reference_codes':[x.code for x in refs],'reference_values':values.ravel().tolist(),'threshold_quantile':.05,'threshold':threshold,'score':score,'bandwidth':.3,'kernel':'gaussian','standardized':True,'seed':42,'grid':grid.tolist(),'pdf':curve.tolist(),'spec':GRADES[tank.grade],'fit_scope':'reference=True且同燃油等级；待测样不参与拟合'}

def fit_fold(values,sample_density):
    """对一组封存参考密度拟合KDE并对待测密度打分；返回(score,threshold)。
    参考不足或无变化时抛ValueError，由调用方记为 insufficient。"""
    if len(values)<MIN_REFERENCES:raise ValueError(f'留一后参考不足{MIN_REFERENCES}条')
    arr=np.array([[float(v)]for v in values],dtype=float)
    scaler=StandardScaler();z=scaler.fit_transform(arr)
    if float(scaler.var_[0])<1e-8:raise ValueError('留一后参考密度没有足够变化')
    model=KernelDensity(kernel='gaussian',bandwidth=.3).fit(z)
    ref_scores=model.score_samples(z)
    threshold=float(np.quantile(ref_scores,.05))
    score=float(model.score_samples(scaler.transform([[float(sample_density)]]))[0])
    grid=np.linspace(float(arr.min()-8),float(arr.max()+8),70)
    curve=(np.exp(model.score_samples(scaler.transform(grid.reshape(-1,1))))/float(scaler.scale_[0])).tolist()
    return score,threshold,{'fit_count':len(arr),'threshold_quantile':.05,'threshold':threshold,'score':score,
                           'bandwidth':.3,'kernel':'gaussian','reference_values':arr.ravel().tolist(),
                           'grid':grid.tolist(),'pdf':curve}
