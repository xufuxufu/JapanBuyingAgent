# ChatGPT 小票识别固定提示词（schema 1.1 批量）

请按照图片顺序识别这些日本购物小票，并只输出一个严格 JSON 对象，不要使用 Markdown 代码块，不要附加解释文字。

规则：

1. 顶层只能包含 `schema_version` 和 `receipts`，`schema_version` 必须是 `1.1`。
2. 每张图片对应 `receipts` 中一个结果；`source_file` 必须逐字复制任务提供的识别文件名，禁止修改、翻译、补全或猜测，`source_page_no` 必须与文件页码一致。
3. 每条商品必须重复填写所属小票的 `source_file`、`source_page_no`，且必须完全一致。
4. 只读取图片中能够确认的内容；不确定字段返回 `null`，不要猜测。
5. 绝对不得猜测、推断或补全 JAN；图片中没有明确 JAN 时，`jan_candidate` 必须为 `null`。
6. 不得补全图片中不存在的商品，也不得把秦丝货号、店内货号或其他编号当成 JAN。
7. 保留小票原始商品简称到 `raw_name`；只有能可靠整理时才填写 `recognized_name`，否则返回 `null`。
8. 数量、单价、折扣、税率、行金额必须分开；所有日元金额必须是整数或 `null`。
9. 每个商品行填写 0～1 的 `confidence`。合计、商品行或税额矛盾时不要修正，在该小票 `warnings` 中说明。
10. `purchased_at`、`receipt_number` 是每个 receipt 的顶层字段；看不清或没有时必须为 `null`。
11. 不得省略任何必需键。若某张图片无法识别，也要返回该 `source_file`，并在 `warnings` 中说明。

严格返回以下结构，并为任务中的每个文件生成一个 receipt：

{
  "schema_version": "1.1",
  "receipts": [
    {
      "source_file": "RCPT-20260715-0028-A7KQ_P01.jpg",
      "source_page_no": 1,
      "store": {"raw_name": ""},
      "purchased_at": null,
      "receipt_number": null,
      "totals": {"subtotal": null, "discount_total": 0, "tax_total": null, "paid_total": null},
      "items": [
        {
          "source_file": "RCPT-20260715-0028-A7KQ_P01.jpg",
          "source_page_no": 1,
          "line_no": 1,
          "raw_name": "",
          "recognized_name": null,
          "jan_candidate": null,
          "quantity": 1,
          "unit_price": null,
          "discount_amount": 0,
          "tax_rate": null,
          "line_total": null,
          "confidence": 0
        }
      ],
      "warnings": []
    }
  ]
}
