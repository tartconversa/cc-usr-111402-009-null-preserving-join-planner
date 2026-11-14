"""受限关系计划的合法重排、代价估计与可审核选择。

在现有执行链(app.baseline、app.semantics、app.storage)之上提供优化器：

- 规范化：过滤谓词合取拆分、常量合取项消除、相邻过滤合并；
- 合法性受检的变换：inner联接交换/结合、过滤按字段所属侧下推、
  保留侧谓词下推过left/semi/anti、谓词可证明拒绝补空行时left转inner；
  semi/anti/left是重排屏障，只递归优化其子计划，不跨保留侧移动；
- 代价估计：仅使用stats与显式默认假设，不读取真实表行数；
- 搜索：inner子图在预算内按子集DP穷举，超预算退化左深贪心并声明未搜索范围；
- 物理对照：优化计划仍由baseline执行，逐节点记录实际行数并做多重集对照。
"""
import copy
import json
import sys

from . import baseline, semantics
from .storage import canonical, digest, freeze, read_json

OPTIMIZER_VERSION = 1
DEFAULT_ROWS = 1000
DEFAULT_NDV = 10
RANGE_SELECTIVITY = 1.0 / 3.0
NULL_SELECTIVITY = 0.1
MAX_EXHAUSTIVE_LEAVES = 8
DEFAULT_BUDGET = 20000

ASSUMPTIONS = {
    'default_rows': DEFAULT_ROWS,
    'default_ndv': DEFAULT_NDV,
    'range_selectivity': RANGE_SELECTIVITY,
    'null_selectivity': NULL_SELECTIVITY,
}


# ---------- 谓词工具 ----------

def conjuncts(predicate):
    """把谓词拆成合取项；None或空and表示不限制，拆为空列表。"""
    if predicate is None:
        return []
    if predicate.get('op') == 'and':
        out = []
        for arg in predicate['args']:
            out.extend(conjuncts(arg))
        return out
    return [predicate]


def conjunction(terms):
    if not terms:
        return None
    if len(terms) == 1:
        return terms[0]
    return {'op': 'and', 'args': list(terms)}


def referenced_fields(expr, acc=None):
    if acc is None:
        acc = set()
    if expr is None:
        return acc
    op = expr['op']
    if op in ('and', 'or'):
        for arg in expr['args']:
            referenced_fields(arg, acc)
    elif op == 'not':
        referenced_fields(expr['arg'], acc)
    else:
        acc.add(expr['field'])
    return acc


# ---------- 计划结构工具 ----------

def schema_of(data, node):
    op = node['op']
    if op == 'scan':
        return list(data['schemas'][node['table']])
    if op == 'filter':
        return schema_of(data, node['input'])
    left = schema_of(data, node['left'])
    if node['kind'] in ('semi', 'anti'):
        return left
    return left + schema_of(data, node['right'])


def walk_plan(node, path='$'):
    yield path, node
    if node['op'] == 'filter':
        yield from walk_plan(node['input'], path + '.input')
    elif node['op'] == 'join':
        yield from walk_plan(node['left'], path + '.left')
        yield from walk_plan(node['right'], path + '.right')


# ---------- 统计与代价估计(只读stats与默认假设，不读真实行数) ----------

class Stats:
    def __init__(self, status, detail, tables):
        self.status = status
        self.detail = detail
        self.tables = tables

    def report(self):
        return {'status': self.status, 'detail': self.detail,
                'assumptions': dict(ASSUMPTIONS), 'tables': self.tables}


def _valid_int(value, minimum):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def load_stats(data):
    raw = data.get('stats')
    if not isinstance(raw, dict) or not isinstance(raw.get('tables'), dict):
        return Stats('missing', 'stats缺失或结构不符，全部使用默认假设', {})
    declared = data.get('snapshot')
    if declared is not None and raw.get('snapshot') != declared:
        return Stats('stale',
                     'stats快照%r与资料声明快照%r不一致，保守退化为默认假设' % (raw.get('snapshot'), declared),
                     {})
    tables = {}
    notes = []
    for name, entry in raw['tables'].items():
        if not isinstance(entry, dict) or not _valid_int(entry.get('rows'), 0):
            notes.append('表%s行数缺失或非法，该表按默认假设' % name)
            continue
        ndv = entry.get('ndv')
        clean = {}
        if isinstance(ndv, dict):
            clean = {f: n for f, n in ndv.items() if _valid_int(n, 1)}
        tables[name] = {'rows': entry['rows'], 'ndv': clean}
    status = 'fresh' if not notes else 'partial'
    return Stats(status, '；'.join(notes) if notes else '统计可用', tables)


class Estimator:
    """基数与选择性估计；只使用Stats与默认假设，不读取真实表内容。"""

    def __init__(self, data, stats):
        self.stats = stats
        self.field_table = {}
        for table, columns in data['schemas'].items():
            for column in columns:
                self.field_table[column] = table

    def rows(self, table):
        entry = self.stats.tables.get(table)
        return entry['rows'] if entry else DEFAULT_ROWS

    def ndv(self, field):
        entry = self.stats.tables.get(self.field_table.get(field))
        if entry:
            return entry['ndv'].get(field, DEFAULT_NDV)
        return DEFAULT_NDV

    def selectivity(self, predicate):
        if predicate is None:
            return 1.0
        op = predicate['op']
        if op == 'and':
            out = 1.0
            for arg in predicate['args']:
                out *= self.selectivity(arg)
            return out
        if op == 'or':
            miss = 1.0
            for arg in predicate['args']:
                miss *= 1.0 - self.selectivity(arg)
            return 1.0 - miss
        if op == 'not':
            return 1.0 - self.selectivity(predicate['arg'])
        if op == 'is_null':
            return NULL_SELECTIVITY
        if op == 'eq':
            return 1.0 / self.ndv(predicate['field'])
        return RANGE_SELECTIVITY

    def join_selectivity(self, on):
        out = 1.0
        for x, y in on:
            out *= 1.0 / max(self.ndv(x), self.ndv(y))
        return out

    def estimate(self, node):
        op = node['op']
        if op == 'scan':
            return float(self.rows(node['table']))
        if op == 'filter':
            return self.estimate(node['input']) * self.selectivity(node['predicate'])
        left = self.estimate(node['left'])
        right = self.estimate(node['right'])
        inner = left * right * self.join_selectivity(node['on'])
        kind = node['kind']
        if kind == 'inner':
            return inner
        if kind == 'left':
            return max(left, inner)
        if kind == 'semi':
            return min(left, inner)
        return max(0.0, left - min(left, inner))


def estimate_walk(estimator, node):
    return [{'path': path, 'op': n['op'], 'est_rows': estimator.estimate(n)}
            for path, n in walk_plan(node)]


def actual_walk(data, node):
    out = []
    for path, n in walk_plan(node):
        rows, _ = baseline.evaluate(data, n)
        out.append({'path': path, 'op': n['op'], 'actual_rows': len(rows)})
    return out


def intermediate_total(walk, key):
    return sum(entry[key] for entry in walk if entry['op'] != 'scan')


# ---------- 合法变换与搜索 ----------

class _AbortRegion(Exception):
    """联接条件或字段布局不允许安全展平inner子图。"""


class Optimizer:
    def __init__(self, data, stats, budget, reorder):
        self.data = data
        self.estimator = Estimator(data, stats)
        self.budget_left = budget
        self.reorder = reorder
        self.steps = []
        self.search = []
        self.field_leaf = {}

    def log(self, rule, **detail):
        self.steps.append({'rule': rule, 'detail': detail})

    def schema(self, node):
        return schema_of(self.data, node)

    def run(self, plan):
        return self.optimize_node(copy.deepcopy(plan))

    def optimize_node(self, node):
        op = node['op']
        if op == 'scan':
            return node
        if op == 'filter':
            child = self.optimize_node(node['input'])
            return self.apply_filter(child, conjuncts(node['predicate']))
        left = self.optimize_node(node['left'])
        right = self.optimize_node(node['right'])
        node = {'op': 'join', 'kind': node['kind'], 'left': left, 'right': right,
                'on': [list(pair) for pair in node['on']]}
        if node['kind'] == 'inner':
            return self.build_region(node)
        return node

    # ----- 过滤下推(合法性在try_push内判定) -----

    def apply_filter(self, node, terms, via_push=False):
        if node['op'] == 'filter':
            self.log('merge-adjacent-filters')
            return self.apply_filter(node['input'], conjuncts(node['predicate']) + terms, via_push)
        remaining = list(terms)
        dirty_region = False
        progressed = True
        while progressed:
            progressed = False
            kept = []
            for term in remaining:
                refs = referenced_fields(term)
                if not refs:
                    value = semantics.evaluate({}, term)
                    if value is True:
                        self.log('drop-constant-conjunct', predicate=term)
                        continue
                    kept.append(term)
                    continue
                pushed = self.try_push(node, term, refs)
                if pushed is None:
                    kept.append(term)
                else:
                    node = pushed
                    progressed = True
                    if node['op'] == 'join' and node['kind'] == 'inner':
                        dirty_region = True
            remaining = kept
        if dirty_region and self.reorder:
            node = self.build_region(node)
        if not remaining:
            if terms:
                self.log('filter-fully-pushed', count=len(terms))
            return node
        if via_push:
            # 每个被下推的合取项只在最终落点记录一次。
            self.log('conjunct-repositioned', predicate=conjunction(remaining), at=self._label(node))
        if node['op'] == 'filter':
            return self.apply_filter(node, remaining, via_push)
        return {'op': 'filter', 'input': node, 'predicate': conjunction(remaining)}

    def try_push(self, node, term, refs):
        if node['op'] != 'join':
            return None
        kind = node['kind']
        left_fields = set(self.schema(node['left']))
        if kind in ('semi', 'anti'):
            # semi/anti只输出左侧行，输出行即左输入行，字段求值结果一致；
            # 只向左(保留侧)下推，不把semi/anti移过任何一侧。
            self.log('push-below-' + kind, predicate=term, fields=sorted(refs))
            node['left'] = self.apply_filter(node['left'], [term], via_push=True)
            return node
        right_fields = set(self.schema(node['right']))
        if kind == 'inner':
            if refs <= left_fields:
                node['left'] = self.apply_filter(node['left'], [term], via_push=True)
                return node
            if refs <= right_fields:
                node['right'] = self.apply_filter(node['right'], [term], via_push=True)
                return node
            return None
        # left join：保留侧字段谓词可下推；补空侧字段谓词只有拒绝补空行时才能转换。
        if refs <= left_fields:
            self.log('push-below-left-preserved', predicate=term, fields=sorted(refs))
            node['left'] = self.apply_filter(node['left'], [term], via_push=True)
            return node
        if refs <= right_fields:
            null_row = dict.fromkeys(self.schema(node['right']))
            value = semantics.evaluate(null_row, term)
            if value is not True:
                # 补空行必被WHERE丢弃，left退化为inner，谓词推入补空侧。
                self.log('left-to-inner-null-rejecting', predicate=term,
                         null_row_result=value, fields=sorted(refs))
                node['kind'] = 'inner'
                node['right'] = self.apply_filter(node['right'], [term], via_push=True)
                return self.build_region(node)
            self.log('keep-above-left-null-accepting', predicate=term, null_row_result=value)
            return None
        return None

    # ----- inner子图展平与重排 -----

    def build_region(self, node):
        if not self.reorder:
            return node
        leaves, edges, residuals = [], [], []
        try:
            self._flatten(node, leaves, edges, residuals)
        except _AbortRegion as exc:
            self.log('region-flatten-aborted', reason=str(exc))
            return node
        if len(leaves) < 3:
            return node
        self.field_leaf = {}
        for index, leaf in enumerate(leaves):
            for field in self.schema(leaf):
                if field in self.field_leaf:
                    self.log('region-flatten-aborted', reason='字段%s跨叶重复，保守保留原结构' % field)
                    return node
                self.field_leaf[field] = index
        report = self._search(leaves, edges)
        tree = report.pop('_tree')
        self.search.append(report)
        rebuilt, _, unused = self._rebuild(tree, leaves, list(edges))
        if unused:
            # 防御：理论上每个联接条件都有唯一落点，异常时保留原结构。
            self.log('region-flatten-aborted', reason='联接条件未能全部落位，保守保留原结构')
            return node
        result = rebuilt
        if residuals:
            self.log('lift-residual-conjuncts', count=len(residuals))
            result = {'op': 'filter', 'input': result, 'predicate': conjunction(residuals)}
        self.log('inner-region-reordered', leaves=len(leaves), strategy=report['strategy'])
        return result

    def _flatten(self, node, leaves, edges, residuals):
        op = node['op']
        if op == 'join' and node['kind'] == 'inner':
            left_fields = self._flatten(node['left'], leaves, edges, residuals)
            right_fields = self._flatten(node['right'], leaves, edges, residuals)
            for pair in node['on']:
                x, y = pair
                if x not in left_fields or y not in right_fields:
                    raise _AbortRegion('联接条件%r字段侧别与原始结构不一致，保守保留原结构' % (pair,))
                edges.append((x, y))
            return left_fields | right_fields
        if op == 'filter':
            inner = node['input']
            if inner['op'] == 'join' and inner['kind'] == 'inner':
                fields = self._flatten(inner, leaves, edges, residuals)
                terms = conjuncts(node['predicate'])
                for term in terms:
                    if not referenced_fields(term) <= fields:
                        raise _AbortRegion('过滤谓词引用了其输入之外的字段，保守保留原结构')
                residuals.extend(terms)
                return fields
            leaves.append(node)
            return set(self.schema(node))
        leaves.append(node)
        return set(self.schema(node))

    def _rebuild(self, tree, leaves, edges):
        if tree[0] == 'leaf':
            return leaves[tree[1]], {tree[1]}, edges
        left_node, left_set, edges = self._rebuild(tree[1], leaves, edges)
        right_node, right_set, edges = self._rebuild(tree[2], leaves, edges)
        on, unused = [], []
        for x, y in edges:
            xl, yl = self.field_leaf[x], self.field_leaf[y]
            if xl in left_set and yl in right_set:
                on.append([x, y])
            elif xl in right_set and yl in left_set:
                on.append([y, x])
            else:
                unused.append((x, y))
        on.sort()
        node = {'op': 'join', 'kind': 'inner', 'left': left_node, 'right': right_node, 'on': on}
        return node, left_set | right_set, unused

    def _label(self, node):
        op = node['op']
        if op == 'scan':
            return 'scan:%s' % node['table']
        if op == 'filter':
            return 'filter>%s' % self._label(node['input'])
        return '%s-join' % node['kind']

    # ----- 联接顺序搜索(预算受限) -----

    def _search(self, leaves, edges):
        n = len(leaves)
        estimator = self.estimator
        leaf_cards = [estimator.estimate(leaf) for leaf in leaves]
        labels = [self._label(leaf) for leaf in leaves]
        pair_sel = [[1.0] * n for _ in range(n)]
        for x, y in edges:
            i, j = self.field_leaf[x], self.field_leaf[y]
            sel = 1.0 / max(estimator.ndv(x), estimator.ndv(y))
            pair_sel[i][j] *= sel
            pair_sel[j][i] *= sel

        def sel_between(set_a, set_b):
            out = 1.0
            for i in set_a:
                for j in set_b:
                    out *= pair_sel[i][j]
            return out

        def bits(mask):
            return [i for i in range(n) if mask & (1 << i)]

        report = {
            'leaves': [{'index': i, 'label': labels[i], 'est_rows': leaf_cards[i]} for i in range(n)],
            'join_edges': [{'left_field': x, 'right_field': y} for x, y in edges],
            'budget': self.budget_left,
        }
        if n <= MAX_EXHAUSTIVE_LEAVES:
            best = {1 << i: (0.0, leaf_cards[i], ('leaf', i)) for i in range(n)}
            combos = 0
            aborted = False
            alternatives = []
            full = (1 << n) - 1
            for size in range(2, n + 1):
                for mask in (m for m in range(1, full + 1) if bin(m).count('1') == size):
                    lowest = mask & (-mask)
                    chosen = None
                    sub = (mask - 1) & mask
                    while sub:
                        if sub & lowest and sub != mask:
                            other = mask ^ sub
                            combos += 1
                            if combos > self.budget_left:
                                aborted = True
                                break
                            cost_a, card_a, tree_a = best[sub]
                            cost_b, card_b, tree_b = best[other]
                            card = card_a * card_b * sel_between(bits(sub), bits(other))
                            cost = cost_a + cost_b + card
                            key = (cost, card, canonical(tree_a) + canonical(tree_b))
                            if chosen is None or key < chosen[0]:
                                chosen = (key, (cost, card, ('join', tree_a, tree_b)))
                            if mask == full:
                                alternatives.append({
                                    'split': [sorted(labels[i] for i in bits(sub)),
                                              sorted(labels[i] for i in bits(other))],
                                    'est_intermediate_rows': cost,
                                })
                        sub = (sub - 1) & mask
                    if aborted:
                        break
                    best[mask] = chosen[1]
                if aborted:
                    break
            if not aborted:
                self.budget_left -= combos
                cost, _, tree = best[full]
                alternatives.sort(key=lambda a: a['est_intermediate_rows'])
                report.update(strategy='exhaustive-dp', combinations=combos,
                              chosen_est_intermediate_rows=cost,
                              alternatives_at_final_choice=alternatives[:10],
                              omitted_alternatives=max(0, len(alternatives) - 10),
                              unsearched=None)
                report['_tree'] = tree
                return report
            self.budget_left = 0
            reason = '组合预算耗尽(已评估%d次，预算%d)' % (combos, report['budget'])
        else:
            reason = '叶数%d超过穷举上限%d' % (n, MAX_EXHAUSTIVE_LEAVES)
        # 左深贪心：每步把估计中间结果最小的叶接到当前树上。
        combos = 0
        remaining = set(range(n))
        current = min(remaining, key=lambda i: (leaf_cards[i], i))
        remaining.discard(current)
        current_set = {current}
        current_card = leaf_cards[current]
        tree = ('leaf', current)
        cost = 0.0
        decisions = []
        while remaining:
            candidates = []
            for i in sorted(remaining):
                sel = sel_between(current_set, {i})
                est_card = current_card * leaf_cards[i] * sel
                candidates.append((est_card, 0 if sel < 1.0 else 1, i))
            combos += len(candidates)
            candidates.sort()
            est_card, _, nxt = candidates[0]
            decisions.append({
                'chosen': labels[nxt],
                'est_intermediate_rows': est_card,
                'alternatives': [{'leaf': labels[i], 'est_intermediate_rows': e}
                                 for e, _, i in candidates[1:6]],
                'omitted_alternatives': max(0, len(candidates) - 6),
            })
            cost += est_card
            current_card = est_card
            current_set.add(nxt)
            remaining.discard(nxt)
            tree = ('join', tree, ('leaf', nxt))
        over = combos > self.budget_left
        self.budget_left = max(0, self.budget_left - combos)
        report.update(strategy='greedy-left-deep', combinations=combos, reason=reason,
                      chosen_est_intermediate_rows=cost, decisions=decisions,
                      unsearched='仅按估计代价贪心扩展左深树；未搜索其余联接顺序与bushy结构'
                                 + ('；贪心本身亦超出剩余预算' if over else ''))
        report['_tree'] = tree
        return report


# ---------- 报告与物理对照 ----------

def optimize(data, budget=DEFAULT_BUDGET):
    """规范化+合法重排，返回可审核报告(不执行计划)。"""
    data = freeze(data)
    stats = load_stats(data)
    normalized = Optimizer(data, stats, budget, reorder=False).run(data['plan'])
    full = Optimizer(data, stats, budget, reorder=True)
    optimized = full.run(data['plan'])
    estimator = Estimator(data, stats)
    walks = {
        'original': estimate_walk(estimator, data['plan']),
        'normalized': estimate_walk(estimator, normalized),
        'optimized': estimate_walk(estimator, optimized),
    }
    return {
        'optimizer_version': OPTIMIZER_VERSION,
        'input_digest': digest(data),
        'budget': budget,
        'objective': '最小化非scan节点估计行数总和(中间结果规模)',
        'stats': stats.report(),
        'steps': full.steps,
        'search': full.search,
        'plans': {'original': data['plan'], 'normalized': normalized, 'optimized': optimized},
        'estimates': walks,
        'intermediate_est_totals': {name: intermediate_total(w, 'est_rows')
                                    for name, w in walks.items()},
    }


def explain(data, budget=DEFAULT_BUDGET):
    """优化并由baseline执行原计划与优化计划，逐节点对照实际行数。"""
    data = freeze(data)
    report = optimize(data, budget)
    original_rows = baseline.evaluate(data, data['plan'])[0]
    optimized_plan = report['plans']['optimized']
    optimized_rows = baseline.evaluate(data, optimized_plan)[0]
    actuals = {
        'original': actual_walk(data, data['plan']),
        'optimized': actual_walk(data, optimized_plan),
    }
    report['actual'] = actuals
    report['intermediate_actual_totals'] = {name: intermediate_total(w, 'actual_rows')
                                            for name, w in actuals.items()}
    report['verification'] = {
        'multiset_equal': sorted(map(canonical, original_rows)) == sorted(map(canonical, optimized_rows)),
        'original_rows': len(original_rows),
        'optimized_rows': len(optimized_rows),
    }
    report['result'] = optimized_rows
    return report


def main():
    args = sys.argv[1:]
    budget = DEFAULT_BUDGET
    if args[:1] == ['--budget']:
        budget = int(args[1])
        args = args[2:]
    if len(args) != 1:
        raise SystemExit('用法: python -m app.optimizer [--budget N] 资料JSON路径')
    report = explain(read_json(args[0]), budget)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
