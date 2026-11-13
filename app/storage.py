"""读取仓湖本地资料并冻结内容身份。"""
import copy
import hashlib
import json
import math
from pathlib import Path

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()

def _check(value):
    if isinstance(value,float) and not math.isfinite(value):raise ValueError('非有限数值')
    if isinstance(value,dict):
        for v in value.values():_check(v)
    elif isinstance(value,list):
        for v in value:_check(v)

def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('JSON字段重复')
        result[key]=value
    return result

def read_json(path):
    value=json.loads(Path(path).read_text(encoding='utf-8'),object_pairs_hook=_pairs)
    return freeze(value)

def freeze(value):
    if not isinstance(value,dict):raise ValueError('资料应为对象')
    _check(value)
    return copy.deepcopy(value)
