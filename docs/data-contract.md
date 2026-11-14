# 资料与接口

## 内容身份

入口app.service.inspect(data)不修改调用方资料，返回input_digest及result。
input_digest是对完整输入JSON按键排序、UTF-8、无空白序列化的SHA-256。数组顺序保留；禁止NaN和无穷，磁盘JSON重复字段拒绝。
app.storage.freeze返回独立副本。输入快照和对象版本由业务字段明确声明，内容摘要不会替调用方创造版本。

## 现有业务约定

plans支持scan、filter、join；列用已限定的a.k形式，schema给每个scan输出字段，字段名跨scan不重叠。join.kind为inner/left/semi/anti，on是字段对普通相等的合取，null不匹配。left缺匹配时补齐右输出schema为null；semi/anti只输出左schema。filter遵循semantics.py，predicate可引用已存在输出字段。baseline.evaluate(data,plan)是保留多重集的直接解释器。stats为可选的表行数、NDV和snapshot，初始程序不用于排序。

## 受限谓词

app.semantics.evaluate(row, expression)返回True/False/None，None为unknown。
eq/lt/le/gt/ge含field和value，缺字段按null；比较双方必须同类型，null比较结果unknown。
is_null只有field；and/or含args数组，not含arg，按三值逻辑计算。空and为true，空or为false。
where只接受结果严格为true的行。expression为null表示不限制。

## 现有执行链

app.service.inspect → storage.freeze/digest → baseline.execute。sample.json描述的是现有可执行输入，服务输出附资料摘要。
新增能力可以增加入口和计划结构，保留现有调用及输出语义；扩展格式应单独声明版本。

## 优化器扩展(optimizer_version=1)

新增app.optimizer与app.service.inspect_optimized，不改变既有入口语义。优化后的计划仍是scan/filter/join结构，由app.baseline执行；报告见app.optimizer.explain(data, budget)。

### stats契约

- stats.tables[t].rows为非负整数行数，stats.tables[t].ndv[f]为正整数不同值数。
- stats.snapshot与资料顶层snapshot(业务字段明确声明)不一致时统计视为陈旧；stats缺失、结构不符或字段非法时同样不可用。
- 不可用时保守退化为默认假设(default_rows=1000、default_ndv=10、range_selectivity=1/3、null_selectivity=0.1)，假设在报告stats.assumptions中列出。
- 估计只读stats与默认假设，不读取真实表内容；实际行数由baseline执行后逐节点统计，列于actual，与估计分列。

### 合法变换依据

- inner联接可交换、结合；过滤合取项可下推到其字段所属的一侧。
- 只引用保留侧字段的谓词可下推过left/semi/anti；semi/anti不跨保留侧移动，left不直接当inner交换。
- 仅当谓词只引用补空侧字段、且经app.semantics求值在全NULL行上不为True(拒绝补空行)时，left转inner并把该谓词推入补空侧；否则谓词保持原位，ON与WHERE位置不互换。
- 联接条件字段侧别与原始结构不一致、字段跨叶重复、或过滤引用其输入之外字段时，放弃展平并保留原结构。
- 报告steps记录每步变换及依据(谓词、字段、补空行求值结果、落点)。

### 搜索与预算

- inner子图叶数不超过8且组合评估不超预算(默认20000)时按子集DP穷举，目标为最小化非scan节点估计行数总和。
- 超预算退化为左深贪心，报告search注明策略、原因、已评估组合数与未搜索范围；最终选择附备选划分及其估计代价。
- 报告plans含original/normalized/optimized；verification给出与现有解释器的多重集对照(重复行、无匹配行、null均按原语义)。
