from datetime import datetime,timezone
from decimal import Decimal
from sqlalchemy import String,Numeric,ForeignKey,Text,Integer,Boolean,UniqueConstraint,CheckConstraint,Index
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base

def _now():return datetime.now(timezone.utc).isoformat()

class CompareRound(Base):
    """按燃油等级开立的比对轮次；每等级同时只有一轮 active。"""
    __tablename__='compare_rounds'
    __table_args__=(CheckConstraint("grade IN ('VLSFO','MGO')"),CheckConstraint("state IN ('active','closed')"),Index('ix_round_grade_active','grade','state'))
    id:Mapped[int]=mapped_column(primary_key=True)
    seq:Mapped[int]=mapped_column(Integer)
    grade:Mapped[str]=mapped_column(String(20))
    state:Mapped[str]=mapped_column(String(20),default='active')
    opened_by:Mapped[str]=mapped_column(String(120))
    opened_at:Mapped[str]=mapped_column(String(40),default=_now)
    closed_at:Mapped[str|None]=mapped_column(String(40),nullable=True)

class RoundReference(Base):
    """受理时封存的参考罐与测值；按 (轮次, 参考样) 唯一，重跑不新增。"""
    __tablename__='round_references'
    __table_args__=(UniqueConstraint('round_id','sample_id',name='uq_round_reference'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    round_id:Mapped[int]=mapped_column(ForeignKey('compare_rounds.id'))
    sample_id:Mapped[int]=mapped_column(ForeignKey('fuel_samples.id'))
    tank_id:Mapped[int]=mapped_column(ForeignKey('tanks.id'))
    code:Mapped[str]=mapped_column(String(70))
    tank_code:Mapped[str]=mapped_column(String(60))
    density_15:Mapped[Decimal]=mapped_column(Numeric(10,3))
    sealed_at:Mapped[str]=mapped_column(String(40),default=_now)

class SampleFoldResult(Base):
    """同一轮次内对每个参考罐逐罐留一的判定结果。"""
    __tablename__='sample_fold_results'
    __table_args__=(UniqueConstraint('sample_id','round_id','held_out_tank_id',name='uq_sample_fold'),
                    CheckConstraint("fold_state IN ('passed','anomaly','insufficient')"))
    id:Mapped[int]=mapped_column(primary_key=True)
    sample_id:Mapped[int]=mapped_column(ForeignKey('fuel_samples.id'))
    round_id:Mapped[int]=mapped_column(ForeignKey('compare_rounds.id'))
    held_out_tank_id:Mapped[int]=mapped_column(ForeignKey('tanks.id'))
    held_out_tank_code:Mapped[str]=mapped_column(String(60))
    fold_state:Mapped[str]=mapped_column(String(20))
    fit_count:Mapped[int]=mapped_column(Integer,default=0)
    score:Mapped[float|None]=mapped_column(Numeric(12,6),nullable=True)
    threshold:Mapped[float|None]=mapped_column(Numeric(12,6),nullable=True)
    spec_ok:Mapped[bool]=mapped_column(Boolean,default=False)
    evidence:Mapped[str]=mapped_column(Text,default='')
    updated_at:Mapped[str]=mapped_column(String(40),default=_now)
