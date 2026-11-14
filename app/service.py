"""对固定内容执行现有查询，并返回资料摘要。"""
import sys
import json
from .storage import freeze,read_json,digest
from .baseline import execute
from . import optimizer

def inspect(data):
    data=freeze(data)
    identity=digest(data)
    result=execute(data)
    return {'input_digest':identity,'result':result}

def inspect_optimized(data,budget=optimizer.DEFAULT_BUDGET):
    """运行优化器并以优化后计划执行；现有inspect语义不变。"""
    data=freeze(data)
    identity=digest(data)
    report=optimizer.explain(data,budget)
    return {'input_digest':identity,'result':report['result'],'optimization':report}

def main():
    if len(sys.argv)!=2:raise SystemExit('请提供资料JSON路径')
    print(json.dumps(inspect(read_json(sys.argv[1])),ensure_ascii=False,allow_nan=False,indent=2))

if __name__=='__main__':main()
