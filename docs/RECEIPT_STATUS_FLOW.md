# Receipt status flow

旧 `receipt_batches.status` 继续兼容历史页面和接口，但不再承载全部流程。新流程使用四个互不覆盖的字段：

- 图片 `image_status`: `uploaded → processing → ready`，不可恢复时为 `failed`。
- GPT `gpt_status`: `not_packaged → zip_downloaded → sent_to_gpt → json_imported → reviewed`。
- GPT 识别任务 `zip_package_jobs.gpt_status`: `zip_ready → zip_downloaded → sent_to_gpt → json_imported/review_pending → reviewed`。一次 ZIP 任务可覆盖多个批次，JSON 只能在该任务内按 `source_file` 严格导入。
- 商品预留 `product_status`: `not_matched / matched / needs_review`。本期不实现匹配。
- 秦丝预留 `qinsi_status`: `not_exported / excel_generated / downloaded / imported / partially_imported / failed`。本期不生成Excel、不导入秦丝。

ZIP下载自动记录首次/最近下载时间和次数；“标记已上传GPT”是人工事件。JSON成功导入会自动补记已上传GPT和导入时间，最终确认记录审核完成时间。

批量 schema 1.1 的只读预览不写数据库；全部任务内图片严格匹配后，确认导入在一个事务内替换所有相关未确认草稿，并为每个涉及批次新增 `ai_recognition_runs` 历史。任何失败都会回滚整次导入。

Image preprocessing warnings, selected recognition source, every accepted raw AI response, and confirmation-time amount warnings are retained. Confirmation never updates inventory, creates products, imports QinSi data, or calls a paid GPT API.

Original bytes are retained permanently. The normal recognition image is the EXIF-corrected high-resolution image; only a high-confidence boundary detection may select a conservative crop by default. Images are not resized unless their longest edge exceeds 7000 pixels. Processing errors safely fall back to the EXIF-corrected original.
