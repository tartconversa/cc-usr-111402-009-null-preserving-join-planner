# 可空维度仓库查询的合法联接重排与代价选择

本项目提供碳预算资料的现有本地执行路径。当前业务行为见app/baseline.py，资料契约见[资料与接口](docs/data-contract.md)。
app/storage.py校验输入并产生确定内容摘要；app/semantics.py提供受限三值谓词；app/service.py绑定资料身份并运行现有代码。
fixtures/sample.json全部为合成资料，不对应真实机构的排放记录。Python 3.14，当前模块仅依赖标准库。

运行已有入口：

    python -m app.service fixtures/sample.json

运行优化器(合法重排、代价选择与对照执行，契约见docs/data-contract.md优化器扩展)：

    python -m app.optimizer fixtures/sample.json
    python -m app.optimizer --budget 500 fixtures/sample.json

程序化入口app.service.inspect_optimized(data, budget)返回input_digest、result及optimization报告。

执行现有行为检查：

    python -m unittest discover -s tests -v

编译检查：

    python -m compileall -q app tests
