from pydantic import BaseModel,Field
from decimal import Decimal
from typing import Literal
class RoundOpenIn(BaseModel):
    grade:Literal['VLSFO','MGO']
    note:str=Field(default='',max_length=500)
class ReferenceSampleIn(BaseModel):
    code:str=Field(min_length=2,max_length=70)
    tank_id:int
    density_15:Decimal=Field(gt=0,le=1500,max_digits=10,decimal_places=3)
    viscosity:Decimal=Field(gt=0,le=1000,max_digits=10,decimal_places=3)
    sulfur:Decimal=Field(ge=0,le=5,max_digits=7,decimal_places=4)
    note:str=Field(default='主管补登参考样',max_length=500)
class RecomputeIn(BaseModel):
    force:bool=Field(default=False,description='True时用封存测值重算留一；False仅用封存结果恢复，不新增判定')
