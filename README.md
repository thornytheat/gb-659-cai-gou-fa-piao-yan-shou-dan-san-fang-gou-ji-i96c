# 采购三方勾稽报告

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18218。测试：docker compose exec -T app pytest -q

## 模块

- M1 单据采信：页面 /documents，接口 /api/documents/import
- M2 主数据映射：页面 /master，接口 /api/master/resolve
- M3 数量勾稽：页面 /quantity，接口 /api/quantity/match
- M4 税额核验：页面 /tax，接口 /api/tax/check
- M5 多对多匹配：页面 /matching，接口 /api/matches/solve
- M6 差异批次：页面 /batches，接口 /api/reconcile（支持外币按原币核销）

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

外币批次：验收成本按收货日汇率、发票应付按开票日汇率分别记账，汇兑差异单独记录；核销按原币进行，同一货号同一币种内先进先出，部分开票只核销对应原币，尾差留在最后一笔，不同币种不互抵。批次页可录入真实验收与发票及各自汇率并对账；并发核销拒绝过期余额，落账失败整批回滚，重跑不新增占用与汇差记录。不做付款结算，不做月末重估。

