"""现有执行路径。"""
from .semantics import where

def evaluate(data,plan):
    op=plan['op']
    if op=='scan':return [dict(r) for r in data['tables'][plan['table']]],list(data['schemas'][plan['table']])
    if op=='filter':
        rows,schema=evaluate(data,plan['input'])
        return [r for r in rows if where(r,plan['predicate'])],schema
    if op!='join':raise ValueError('未知关系算子')
    left,ls=evaluate(data,plan['left']);right,rs=evaluate(data,plan['right'])
    if set(ls)&set(rs):raise ValueError('关系字段重名')
    kind=plan['kind'];result=[]
    if kind not in ('inner','left','semi','anti'):raise ValueError('未知联接')
    for a in left:
        matches=[b for b in right if all(a.get(x) is not None and b.get(y) is not None and a[x]==b[y] for x,y in plan['on'])]
        if kind in ('inner','left'):
            result.extend({**a,**b} for b in matches)
            if kind=='left' and not matches:result.append({**a,**dict.fromkeys(rs)})
        elif (kind=='semi' and matches) or (kind=='anti' and not matches):result.append(a)
    return result,ls if kind in ('semi','anti') else ls+rs

def execute(data):return evaluate(data,data['plan'])[0]
