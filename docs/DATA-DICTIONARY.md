# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。
- 原币（original_amount）：验收与发票按自身币种记录的金额，始终按字符串保存，序列化保留两位小数。
- 汇率：原币到人民币的换算率，使用 Decimal；验收保存收货日汇率，发票保存开票日汇率。
- cost_cny：验收成本 = 原币 × 收货日汇率，金额半入到分。
- payable_cny：发票应付 = 原币 × 开票日汇率，金额半入到分；尾差并入最后一笔分摊。
- fx_difference_cny：汇兑差异 = payable_cny − cost_cny，逐笔记录，不与未开票金额混同。
- writeoff：按原币记录的一次验收占用，包含 amount、cost_cny、payable_cny、fx_difference_cny 与序号。
- open_amount / open_cost_cny：验收尚未核销的原币余额与对应人民币成本余额。
- version：验收行的核销版本号，每次落账递增；请求可带 version 或 expected_balance 作为并发校验。
- reason_code：外币批次新增 `FX_DIFFERENCE`（汇率差异）与 `UNINVOICED`（未开票），另有 `STALE_BALANCE`、`INSUFFICIENT_ACCEPTANCE`、`BAD_CURRENCY` 等拒绝原因。
- 币种隔离：SKU + currency 是最小核销分组；不同币种永不互抵，汇总按币种分别列示。

