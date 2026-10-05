"""코드를 실행하지 않는 제한 수식 계산과 독립 정밀도 검산."""
from __future__ import annotations
import ast
from decimal import Decimal, localcontext
import hashlib
import math
import re
import keyword
from pydantic import Field
from .schemas import StrictModel, new_id
from .database import to_json
from .control_plane import ControlError

class Calculation(StrictModel):
    name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,60}$')
    expression: str = Field(min_length=1,max_length=300)
    unit: str = Field(min_length=1,max_length=80)

class CalculationPlan(StrictModel):
    inputs: dict[str,float] = Field(max_length=25)
    calculations: list[Calculation] = Field(min_length=1,max_length=16)

def evaluate(expression, values, *, precise=False):
    values=dict(values)
    for i,name in enumerate(list(values)):
        if keyword.iskeyword(name):
            alias=f'__input_{i}';expression=re.sub(r'\b'+re.escape(name)+r'\b',alias,expression);values[alias]=values.pop(name)
    try:tree=ast.parse(expression,mode='eval')
    except SyntaxError:raise ValueError('EXPRESSION_SYNTAX_INVALID') from None
    if len(list(ast.walk(tree)))>80:raise ValueError('EXPRESSION_LIMIT')
    def walk(n,depth=0):
        if depth>12:raise ValueError('EXPRESSION_LIMIT')
        if isinstance(n,ast.Constant) and type(n.value) in (int,float):
            if not math.isfinite(n.value) or abs(n.value)>1e30:raise ValueError('NUMBER_LIMIT')
            return Decimal(str(n.value)) if precise else float(n.value)
        if isinstance(n,ast.Name) and n.id in values:return values[n.id]
        if isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.UAdd,ast.USub)):
            value=walk(n.operand,depth+1);return value if isinstance(n.op,ast.UAdd) else -value
        if isinstance(n,ast.BinOp):
            a,b=walk(n.left,depth+1),walk(n.right,depth+1)
            if isinstance(n.op,ast.Add):v=a+b
            elif isinstance(n.op,ast.Sub):v=a-b
            elif isinstance(n.op,ast.Mult):v=a*b
            elif isinstance(n.op,ast.Div):v=a/b
            elif isinstance(n.op,ast.Pow) and abs(b)<=100:v=a**b
            else:raise ValueError('OPERATION_DENIED')
            if not math.isfinite(float(v)) or abs(v)>1e30:raise ValueError('NUMBER_LIMIT')
            return v
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in {'log','sqrt','exp'} and len(n.args)==1 and not n.keywords:
            value=walk(n.args[0],depth+1)
            if n.func.id=='log':return value.ln() if precise else math.log(value)
            if n.func.id=='sqrt':return value.sqrt() if precise else math.sqrt(value)
            if abs(value)>50:raise ValueError('NUMBER_LIMIT')
            return value.exp() if precise else math.exp(value)
        raise ValueError('EXPRESSION_DENIED')
    with localcontext() as ctx:
        ctx.prec=60;return walk(tree.body)

def compute(plan):
    floats={k:float(v) for k,v in plan.inputs.items()};precise={k:Decimal(str(v)) for k,v in plan.inputs.items()}
    if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,60}',k) or not math.isfinite(v) for k,v in floats.items()):raise ValueError('INPUT_INVALID')
    results=[]
    for item in plan.calculations:
        if item.name in floats:raise ValueError('DUPLICATE_NAME')
        first=evaluate(item.expression,floats);second=evaluate(item.expression,precise,precise=True)
        if not math.isclose(first,float(second),rel_tol=1e-10,abs_tol=1e-10):raise ValueError('ARITHMETIC_DISAGREEMENT')
        floats[item.name]=first;precise[item.name]=second
        results.append({'field':item.name,'value':first,'unit':item.unit,'expression':item.expression,'decimal_value':str(second),
            'sentence':f'검산된 계산 {item.name}: {first:.12g} {item.unit}.'})
    return {'plan':plan.model_dump(mode='json'),'results':results,'verification':'FLOAT_AND_DECIMAL_AGREE','unit_validation':'MODEL_DECLARED_UNITS'}

def save_calculation(state,rid,contract_id,plan):
    result=compute(plan);aid=new_id('ART');relative=f'results/{aid}-calculation.json'
    data=(to_json(result)+'\n').encode('utf-8',errors='strict');state.workspace.path(rid,relative).write_bytes(data)
    state.register_file_artifact(aid,rid,contract_id,'SCIENTIFIC_CALCULATION',relative,'tool',contract_id,hashlib.sha256(data).hexdigest())
    return {'status':'VERIFIED','artifact_id':aid,**result}

def checked_calculations(state,rid):
    import json
    records=[]
    for row in state._db.execute("SELECT artifact_id FROM artifacts WHERE research_id=? AND artifact_type='SCIENTIFIC_CALCULATION' AND status NOT IN ('INVALIDATED','SUPERSEDED') ORDER BY rowid",(rid,)):
        artifact=state.file_artifact(row[0],rid)
        value=json.loads(state.workspace.path(rid,artifact['relative_path']).read_text(encoding='utf-8'))
        expected=compute(CalculationPlan.model_validate(value['plan']))
        if expected!=value:raise ControlError('CALCULATION_TAMPERED')
        records.append({'artifact_id':row[0],'sha256':artifact['sha256'],**value})
    from .scientific_dataset_compute import checked_dataset_calculations
    return records+checked_dataset_calculations(state,rid)
