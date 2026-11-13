"""现有接口和关键业务语义检查。"""
import copy
import json
import tempfile
from pathlib import Path
import unittest
from app.storage import freeze,read_json,digest
from app.service import inspect
from app.semantics import evaluate,where

SAMPLE=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'sample.json').read_text(encoding='utf-8'))
EXPECTED=[{'a.k': 1, 'a.v': 'x', 'b.k': 1, 'b.v': 'ok'}, {'a.k': 2, 'a.v': 'y', 'b.k': None, 'b.v': None}]

class ExistingBehaviorTests(unittest.TestCase):
    def test_existing_query_result(self):
        actual=inspect(SAMPLE)
        self.assertEqual(actual['result'],EXPECTED)
        self.assertEqual(actual['input_digest'],digest(SAMPLE))

    def test_input_and_output_isolation(self):
        before=copy.deepcopy(SAMPLE)
        frozen=freeze(SAMPLE)
        frozen['unrelated']='changed'
        result=inspect(SAMPLE)
        result['result'].clear()
        self.assertEqual(SAMPLE,before)
        self.assertEqual(inspect(SAMPLE)['result'],EXPECTED)

    def test_input_identity_and_invalid_number(self):
        data=copy.deepcopy(SAMPLE);data['marker']='new'
        self.assertNotEqual(digest(data),digest(SAMPLE))
        data['marker']=float('nan')
        with self.assertRaisesRegex(ValueError,'非有限'):freeze(data)

    def test_json_duplicate_key_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'input.json'
            path.write_text('{"a":1,"a":2}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'重复'):read_json(path)

    def test_threevalued_filter(self):
        unknown={'op':'eq','field':'a','value':1}
        self.assertIsNone(evaluate({'a':None},unknown))
        self.assertFalse(where({'a':None},unknown))
        self.assertTrue(evaluate({'a':None},{'op':'is_null','field':'a'}))
        self.assertIsNone(evaluate({'a':None},{'op':'not','arg':unknown}))
        self.assertIs(evaluate({'a':None,'x':0},{'op':'and','args':[unknown,{'op':'eq','field':'x','value':1}]}),False)

    def test_where_is_not_on_and_duplicates_remain(self):
        from app.baseline import evaluate
        data=copy.deepcopy(SAMPLE)
        filtered={'op':'filter','input':data['plan'],'predicate':{'op':'eq','field':'b.v','value':'ok'}}
        self.assertEqual(len(evaluate(data,filtered)[0]),1)
        data['tables']['b'].append(copy.deepcopy(data['tables']['b'][0]))
        rows,schema=evaluate(data,data['plan'])
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[-1]['b.k'],None)
        data['plan']['kind']='anti'
        self.assertEqual(evaluate(data,data['plan'])[0],[{'a.k':2,'a.v':'y'}])

if __name__=='__main__':unittest.main()
