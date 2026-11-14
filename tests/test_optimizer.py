"""优化器的合法性、估计与搜索行为检查。"""
import json
import unittest
from pathlib import Path

from app.optimizer import explain, optimize, DEFAULT_ROWS
from app.service import inspect_optimized
from app.storage import digest

SAMPLE = json.loads((Path(__file__).resolve().parents[1] / 'fixtures' / 'sample.json').read_text(encoding='utf-8'))
EXPECTED = [{'a.k': 1, 'a.v': 'x', 'b.k': 1, 'b.v': 'ok'}, {'a.k': 2, 'a.v': 'y', 'b.k': None, 'b.v': None}]


def scan(table):
    return {'op': 'scan', 'table': table}


def jn(kind, left, right, on):
    return {'op': 'join', 'kind': kind, 'left': left, 'right': right, 'on': on}


def flt(predicate, node):
    return {'op': 'filter', 'input': node, 'predicate': predicate}


def eq(field, value):
    return {'op': 'eq', 'field': field, 'value': value}


def rich_data():
    tables = {
        'a': [{'a.k': 1, 'a.v': 'x'}, {'a.k': 2, 'a.v': 'y'},
              {'a.k': 2, 'a.v': 'y2'}, {'a.k': None, 'a.v': 'n'}],
        'b': [{'b.k': 1, 'b.v': 'ok'}, {'b.k': 2, 'b.v': 'ok'},
              {'b.k': 2, 'b.v': 'no'}, {'b.k': None, 'b.v': 'z'}],
        'c': [{'c.k': 1, 'c.w': 10}, {'c.k': 2, 'c.w': 20},
              {'c.k': 2, 'c.w': 21}, {'c.k': 3, 'c.w': None}],
        'd': [{'d.k': 1, 'd.u': 'p'}, {'d.k': 2, 'd.u': 'q'}, {'d.k': 2, 'd.u': 'q2'}],
    }
    schemas = {name: sorted({key for row in rows for key in row}) for name, rows in tables.items()}
    return tables, schemas


def make_data(plan, tables=None, schemas=None, stats=None, snapshot=None):
    if tables is None:
        tables, schemas = rich_data()
    data = {'tables': tables, 'schemas': schemas,
            'stats': stats if stats is not None else {}, 'plan': plan}
    if snapshot is not None:
        data['snapshot'] = snapshot
    return data


def all_nodes(plan):
    yield plan
    if plan['op'] == 'filter':
        yield from all_nodes(plan['input'])
    elif plan['op'] == 'join':
        yield from all_nodes(plan['left'])
        yield from all_nodes(plan['right'])


def rules(report):
    return [step['rule'] for step in report['steps']]


def plans_battery():
    a, b, c, d = scan('a'), scan('b'), scan('c'), scan('d')
    return {
        'inner_chain_filter': flt({'op': 'and', 'args': [eq('a.v', 'x'), {'op': 'lt', 'field': 'c.w', 'value': 25}]},
                                  jn('inner', jn('inner', a, b, [['a.k', 'b.k']]), c, [['b.k', 'c.k']])),
        'left_null_rejecting': flt(eq('b.v', 'ok'), jn('left', a, b, [['a.k', 'b.k']])),
        'left_null_accepting': flt({'op': 'is_null', 'field': 'b.v'}, jn('left', a, b, [['a.k', 'b.k']])),
        'semi_then_inner': jn('inner', jn('semi', a, b, [['a.k', 'b.k']]), c, [['a.k', 'c.k']]),
        'anti_filter': flt(eq('a.v', 'x'), jn('anti', a, b, [['a.k', 'b.k']])),
        'left_inside_inner': jn('inner', jn('left', a, b, [['a.k', 'b.k']]), c, [['a.k', 'c.k']]),
        'cross_product': jn('inner', jn('inner', a, b, [['a.k', 'b.k']]), c, []),
        'spanning_filter': flt({'op': 'or', 'args': [eq('a.v', 'x'), eq('b.v', 'ok')]},
                               jn('inner', a, b, [['a.k', 'b.k']])),
        'degenerate_on': jn('inner', a, b, [['b.k', 'a.k']]),
        'four_way': jn('inner', jn('inner', jn('inner', a, b, [['a.k', 'b.k']]), c, [['b.k', 'c.k']]),
                       d, [['c.k', 'd.k']]),
        'left_chain': jn('left', jn('left', a, b, [['a.k', 'b.k']]), c, [['a.k', 'c.k']]),
        'semi_right_inner': jn('semi', a, jn('inner', b, c, [['b.k', 'c.k']]), [['a.k', 'b.k']]),
    }


def chain_data(n):
    tables = {'t%d' % i: [{'t%d.k' % i: 1}, {'t%d.k' % i: 2}] for i in range(n)}
    schemas = {'t%d' % i: ['t%d.k' % i] for i in range(n)}
    plan = scan('t0')
    for i in range(1, n):
        plan = jn('inner', plan, scan('t%d' % i), [['t%d.k' % (i - 1), 't%d.k' % i]])
    return make_data(plan, tables, schemas)


class MultisetPreservationTests(unittest.TestCase):
    def test_all_plans_match_baseline_multiset(self):
        for name, plan in plans_battery().items():
            with self.subTest(plan=name):
                report = explain(make_data(plan))
                self.assertTrue(report['verification']['multiset_equal'], report['steps'])
                v = report['verification']
                self.assertEqual(v['original_rows'], v['optimized_rows'])

    def test_empty_tables_match_baseline(self):
        tables, schemas = rich_data()
        tables['b'] = []
        for name in ('left_null_rejecting', 'left_null_accepting', 'anti_filter'):
            with self.subTest(plan=name):
                report = explain(make_data(plans_battery()[name], tables, schemas))
                self.assertTrue(report['verification']['multiset_equal'])

    def test_duplicates_nulls_unmatched_preserved(self):
        report = explain(make_data(plans_battery()['inner_chain_filter']))
        self.assertTrue(report['verification']['multiset_equal'])
        # a.k=2在a、b、c中各有两行匹配，a.v='x'只留a.k=1：结果应为1行
        self.assertEqual(report['verification']['optimized_rows'], 1)


class LegalityTests(unittest.TestCase):
    def test_left_to_inner_only_when_null_rejecting(self):
        report = explain(make_data(plans_battery()['left_null_rejecting']))
        self.assertIn('left-to-inner-null-rejecting', rules(report))
        kinds = {n.get('kind') for n in all_nodes(report['plans']['optimized']) if n['op'] == 'join'}
        self.assertNotIn('left', kinds)
        self.assertEqual(len(report['result']), 3)  # (1,x),(2,y),(2,y2)各保留b.v='ok'一行

    def test_left_kept_when_null_accepting(self):
        report = explain(make_data(plans_battery()['left_null_accepting']))
        self.assertIn('keep-above-left-null-accepting', rules(report))
        self.assertNotIn('left-to-inner-null-rejecting', rules(report))
        optimized = report['plans']['optimized']
        self.assertEqual(optimized['op'], 'filter')  # 谓词留在left join之上
        join = optimized['input']
        self.assertEqual(join['kind'], 'left')
        self.assertEqual(join['on'], [['a.k', 'b.k']])  # ON位置不变
        self.assertEqual(report['result'],
                         [{'a.k': None, 'a.v': 'n', 'b.k': None, 'b.v': None}])

    def test_filter_pushes_below_inner(self):
        report = explain(make_data(plans_battery()['inner_chain_filter']))
        self.assertEqual(rules(report).count('conjunct-repositioned'), 2)
        last_search = report['search'][-1]
        leaf_est = {leaf['label']: leaf['est_rows'] for leaf in last_search['leaves']}
        self.assertEqual(leaf_est['filter>scan:a'], DEFAULT_ROWS * 0.1)  # 下推后的叶估计变小

    def test_filter_pushes_below_anti_preserved_side(self):
        report = explain(make_data(plans_battery()['anti_filter']))
        self.assertIn('push-below-anti', rules(report))
        anti = next(n for n in all_nodes(report['plans']['optimized']) if n.get('kind') == 'anti')
        self.assertEqual(anti['left']['op'], 'filter')

    def test_semi_not_moved_across_join(self):
        report = explain(make_data(plans_battery()['semi_then_inner']))
        semi = next(n for n in all_nodes(report['plans']['optimized']) if n.get('kind') == 'semi')
        self.assertEqual(semi['left'], scan('a'))  # 保留侧结构不动
        self.assertEqual(semi['right'], scan('b'))
        self.assertNotIn('conjunct-repositioned', rules(report))

    def test_spanning_conjunct_stays_above(self):
        report = explain(make_data(plans_battery()['spanning_filter']))
        self.assertEqual(report['plans']['optimized']['op'], 'filter')
        self.assertTrue(report['verification']['multiset_equal'])

    def test_degenerate_on_aborts_flatten(self):
        report = explain(make_data(plans_battery()['degenerate_on']))
        self.assertIn('region-flatten-aborted', rules(report))
        self.assertEqual(report['result'], [])  # 与baseline一致：侧别不符永不匹配

    def test_residual_filter_lifted_legally(self):
        plan = jn('inner', flt({'op': 'or', 'args': [eq('a.v', 'x'), eq('b.v', 'no')]},
                               jn('inner', scan('a'), scan('b'), [['a.k', 'b.k']])),
                  scan('c'), [['b.k', 'c.k']])
        report = explain(make_data(plan))
        self.assertIn('lift-residual-conjuncts', rules(report))
        self.assertTrue(report['verification']['multiset_equal'])


class StatsAndEstimationTests(unittest.TestCase):
    def simple_plan(self):
        return flt(eq('a.v', 'x'), scan('a'))

    def test_missing_stats_uses_declared_defaults(self):
        report = optimize(make_data(self.simple_plan()))
        self.assertEqual(report['stats']['status'], 'missing')
        est = {e['path']: e['est_rows'] for e in report['estimates']['original']}
        self.assertEqual(est['$.input'], float(DEFAULT_ROWS))
        self.assertIn('default_rows', report['stats']['assumptions'])

    def test_stale_stats_degrade_conservatively(self):
        stats = {'snapshot': 'old', 'tables': {'a': {'rows': 4, 'ndv': {'a.v': 2}}}}
        report = optimize(make_data(self.simple_plan(), stats=stats, snapshot='new'))
        self.assertEqual(report['stats']['status'], 'stale')
        est = {e['path']: e['est_rows'] for e in report['estimates']['original']}
        self.assertEqual(est['$.input'], float(DEFAULT_ROWS))

    def test_fresh_stats_used(self):
        stats = {'snapshot': 's1', 'tables': {'a': {'rows': 4, 'ndv': {'a.v': 2}}}}
        report = optimize(make_data(self.simple_plan(), stats=stats, snapshot='s1'))
        self.assertEqual(report['stats']['status'], 'fresh')
        est = {e['path']: e['est_rows'] for e in report['estimates']['original']}
        self.assertEqual(est['$.input'], 4.0)
        self.assertEqual(est['$'], 4.0 * 0.5)

    def test_estimates_do_not_read_real_rows(self):
        stats = {'snapshot': 's1', 'tables': {'a': {'rows': 4, 'ndv': {'a.v': 2}}}}
        plan = self.simple_plan()
        first = optimize(make_data(plan, stats=stats, snapshot='s1'))
        tables, schemas = rich_data()
        tables['a'] = tables['a'] * 100  # 真实行数放大，stats不变
        second = optimize(make_data(plan, tables, schemas, stats=stats, snapshot='s1'))
        self.assertEqual(first['estimates'], second['estimates'])

    def test_actual_rows_reported_per_node(self):
        report = explain(make_data(plans_battery()['four_way']))
        self.assertIn('actual', report)
        optimized = {e['op'] for e in report['actual']['optimized']}
        self.assertEqual(optimized, {'scan', 'join'})
        totals = report['intermediate_actual_totals']
        self.assertIn('original', totals)
        self.assertIn('optimized', totals)


class SearchBudgetTests(unittest.TestCase):
    def test_exhaustive_dp_within_budget(self):
        report = explain(make_data(plans_battery()['four_way']))
        last = report['search'][-1]
        self.assertEqual(last['strategy'], 'exhaustive-dp')
        self.assertEqual(len(last['leaves']), 4)
        self.assertIsNone(last['unsearched'])
        self.assertTrue(last['alternatives_at_final_choice'])  # 最终选择有可审核备选

    def test_greedy_when_leaves_exceed_limit(self):
        report = explain(chain_data(9))
        last = report['search'][-1]
        self.assertEqual(last['strategy'], 'greedy-left-deep')
        self.assertIn('超过穷举上限', last['reason'])
        self.assertIn('未搜索', last['unsearched'])
        self.assertTrue(report['verification']['multiset_equal'])

    def test_greedy_when_budget_exhausted(self):
        report = explain(chain_data(4), budget=1)
        last = report['search'][-1]
        self.assertEqual(last['strategy'], 'greedy-left-deep')
        self.assertIn('预算', last['reason'])
        self.assertTrue(report['verification']['multiset_equal'])

    def test_choice_is_auditable_not_template(self):
        report = optimize(make_data(plans_battery()['four_way']))
        self.assertEqual(report['objective'], '最小化非scan节点估计行数总和(中间结果规模)')
        totals = report['intermediate_est_totals']
        self.assertLessEqual(totals['optimized'], totals['original'])


class ServiceEntryTests(unittest.TestCase):
    def test_inspect_optimized_sample(self):
        actual = inspect_optimized(SAMPLE)
        self.assertEqual(actual['result'], EXPECTED)
        self.assertEqual(actual['input_digest'], digest(SAMPLE))
        self.assertTrue(actual['optimization']['verification']['multiset_equal'])
        self.assertEqual(actual['optimization']['optimizer_version'], 1)


if __name__ == '__main__':
    unittest.main()
