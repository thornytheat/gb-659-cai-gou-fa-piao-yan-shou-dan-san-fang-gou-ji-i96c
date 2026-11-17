# 深度登记 采购三方勾稽报告

- 规模：中型
- 核心规则：采购、分批验收和合并发票按货号与税率分组，数量和金额使用 Decimal 守恒。
- 使用者与工作流：应付会计导入三类单据，映射主数据，核对数量税额，多对多匹配并导出差异。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 单据采信 | 导入三类单据并隔离冲突 | POST /api/documents/import | `app/modules/m1_documents/parser.py` `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py` |
| M2 主数据映射 | 归一货号与旧字段 | POST /api/master/resolve | `app/modules/m2_master/aliases.py` `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py` |
| M3 数量勾稽 | 统一包装单位并核对未到量 | POST /api/quantity/match | `app/modules/m3_quantity/units.py` `app/modules/m3_quantity/match.py` | `tests/test_m3_quantity.py` |
| M4 税额核验 | 重算净额、税额与含税额 | POST /api/tax/check | `app/modules/m4_tax/calculate.py` `app/modules/m4_tax/verify.py` | `tests/test_m4_tax.py` |
| M5 多对多匹配 | 在相同货号税率内组合匹配 | POST /api/matches/solve | `app/modules/m5_matching/candidates.py` `app/modules/m5_matching/solver.py` | `tests/test_m5_matching.py` |
| M6 差异批次 | 核对金额守恒并导出差异 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/fx.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` `tests/test_m6_fx.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-05 | 单据缺少字段 | 静默补空进入匹配 | 隔离并保留行号 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-06 | 金额带千分位 | 直接转换失败 | 清理分隔符后使用 Decimal | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_thousands_amount_uses_decimal` |
| TRAP-07 | 空值哨兵 | 被当作零金额 | 空值集合统一拒绝 | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-08 | 供应商编码前导零 | 转整数后主数据失配 | 编码始终按字符串保形 | `app/modules/m2_master/aliases.py` | `tests/test_m2_master.py::test_supplier_code_preserves_leading_zero` |
| TRAP-10 | 税额浮点尾差 | 分项和总额差一分 | Decimal 半入到分 | `app/modules/m4_tax/calculate.py` | `tests/test_m4_tax.py::test_half_up_to_cent` |
| TRAP-14 | 重复单号值冲突 | 后写覆盖金额 | 单独隔离冲突版本 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_conflicting_duplicate_detected` |
| TRAP-17 | 旧列名仍在使用 | 新版读取报错 | 受控字段迁移并报告冲突 | `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py::test_old_columns_migrate` |
| TRAP-19 | 组合候选顺序变化 | 匹配结果随输入顺序变化 | 按差额、数量与编号稳定择一 | `app/modules/m5_matching/solver.py` | `tests/test_m5_matching.py::test_choice_is_order_independent` |
| TRAP-23 | 相邻税率边界 | 不同税率金额互相抵消 | 货号与税率共同分组 | `app/modules/m4_tax/verify.py` | `tests/test_m4_tax.py::test_different_tax_rates_never_offset` |
| TRAP-26 | 同批重复执行 | 重复新增对账结果 | 输入摘要作为运行唯一键 | `app/store.py` | `tests/test_m6_batches.py::test_idempotent_record` |
| TRAP-27 | 外币发票按开票日汇率 | 统一改价，存货成本被开票日汇率覆盖 | 成本保留收货日汇率，应付按开票日汇率，两者分开 | `app/modules/m6_batches/fx.py` | `tests/test_m6_fx.py::test_cost_keeps_receipt_rate_and_payable_uses_invoice_rate` |
| TRAP-28 | 直接比人民币金额 | 汇兑差被误判为少开票 | 按原币核销，汇兑差异单独落账 | `app/modules/m6_batches/fx.py` | `tests/test_m6_fx.py::test_fx_difference_is_not_under_invoicing` |
| TRAP-29 | 部分开票 | 整行验收被核销或改价 | 仅核销对应原币，余量保持开放 | `app/modules/m6_batches/fx.py` | `tests/test_m6_fx.py::test_partial_invoice_only_consumes_matching_original_amount` |
| TRAP-30 | 分币舍入尾差 | 尾差分散到各行，成本出现负数 | 累计确认，尾差留在最后一笔 | `app/modules/m6_batches/fx.py` | `tests/test_m6_fx.py::test_rounding_residual_stays_on_last_allocation` |
| TRAP-31 | 多币种同货号 | 不同币种金额互相抵消 | 币种不互抵，按币种分别核销与汇总 | `app/modules/m6_batches/fx.py` | `tests/test_m6_fx.py::test_currencies_never_offset` |
| TRAP-32 | 并发核销同一余额 | 双写导致超额核销 | 写事务加锁并校验过期余额/版本 | `app/store.py` `app/modules/m6_batches/service.py` | `tests/test_m6_fx.py::test_stale_balance_is_rejected` |
| TRAP-33 | 落账中途失败 | 半批次写入残留 | 整批事务回滚，原核销保持不变 | `app/store.py` | `tests/test_m6_fx.py::test_failure_after_partial_insert_rolls_back_everything` |
| TRAP-34 | 重跑同批 | 新增验收占用与汇差记录 | 运行唯一键复用，不重复写入 | `app/modules/m6_batches/service.py` | `tests/test_m6_fx.py::test_rerun_does_not_duplicate_writeoffs_or_fx_rows` |

## 对账规则

- 采购数量 = 净验收数量 + 未到数量。
- 净验收含税额 = 已开票含税额 + 未开票金额 + 已解释差异。
- 不同税率的行不可相互抵消。
- 外币：验收成本 = 原币 × 收货日汇率；发票应付 = 原币 × 开票日汇率；汇兑差异 = 应付 − 成本，单独记录。
- 核销以原币进行，同一货号 + 同一币种内按收货日先进先出；部分开票只占用对应原币，尾差留在最后一笔。
- 每个币种内满足：验收原币 = 已核销原币 + 未开票原币；不同币种不互抵、不合并。

## 已知不覆盖

- 不做付款结算（核销只到应付确认，不生成付款单）。
- 不做月末重估（汇兑差异按核销时点记录，不按期末汇率重估余额）。

