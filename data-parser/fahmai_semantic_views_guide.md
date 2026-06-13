# FahMai Semantic Views Guide

Source database: `fahmai_tables.duckdb`

ไฟล์นี้สรุป views ที่สร้างไว้เพื่อลดงานของ LLM เวลาแปลงคำถามเป็น SQL โดยแบ่งเป็น 3 กลุ่ม:

- **Enriched views**: join fact/dim หลักไว้ให้แล้ว
- **Lookup-expanded views**: ขยาย effective date / campaign window เป็นรายวัน เพื่อไม่ต้องเขียน logic as-of เอง
- **Answer-ready summary views**: aggregate/dedup/reconciliation เตรียมไว้สำหรับคำถามที่เจอบ่อย

ดูรายการ views ทั้งหมดใน DuckDB:

```sql
SELECT view_name, base_table, row_count, description
FROM semantic_view_manifest
ORDER BY view_name;
```

## Enriched Views

| View | ใช้เมื่อคำถามถามเรื่อง | ประโยชน์ต่อ LLM |
| --- | --- | --- |
| `vw_sales_enriched` | ยอดขายระดับ transaction, fiscal year, branch, channel, B2B/B2C, payment, promo, settlement bank | ไม่ต้อง join `FACT_SALES` กับ date/branch/customer/employee/promo/bank เอง |
| `vw_sales_line_enriched` | SKU/item-level sales, brand/category revenue, care plus, line discount, quantity | ใช้ตอบคำถามสินค้าได้ตรงกว่า `FACT_SALES` เพราะมี `sku_id` และ product context |
| `vw_promo_redemption_enriched` | campaign redemption, discount, campaign window, redemption channel, sales cohort | รวม `FACT_PROMO_REDEMPTION` + campaign + sales ไว้แล้ว |
| `vw_returns_refunds_enriched` | return/refund, SKU ที่ refund มากสุด, refund bank transaction, approver | รวม return, refund, product, original sale, bank transaction |
| `vw_bank_transaction_enriched` | bank statement, account, counterparty, related entity, balance | รวม bank transaction กับ bank account และ branch context |
| `vw_vendor_payment_enriched` | vendor invoice/payment, signer/cosigner, contract version, bank transaction | ใช้ตอบ invoice/payment แบบไม่ต้อง join 5 ตารางเอง |
| `vw_customer_service_enriched` | CS interaction, refund/warranty ที่ผูกกับเคส, employee/branch/channel | ใช้ bridge ระหว่าง CS กับ refund/warranty |
| `vw_warranty_claim_enriched` | warranty claim, original sale, SKU/customer/product warranty | รวม claim กับ product และ transaction ต้นทาง |
| `vw_loyalty_ledger_enriched` | loyalty points, resulting tier, ledger event ที่ผูกกับ sale | รวม customer + sales context ไว้แล้ว |
| `vw_inventory_movement_enriched` | stock movement ราย SKU/branch/type | ใช้กับคำถามรับเข้า/ขายออก/ปรับ stock |
| `vw_inventory_snapshot_enriched` | month-end stock, closing units ราย SKU/branch | ใช้กับคำถาม inventory สิ้นเดือน |
| `vw_payroll_enriched` | payroll, department/branch/employee, bank txn ที่จ่ายเงินเดือน | ใช้กับ HR/Finance payroll reconciliation |
| `vw_shipping_enriched` | shipping, tracking number, vendor ขนส่ง, destination province | รวม shipment กับ sale/vendor/branch |
| `vw_product_enriched` | product master, vendor, category, warranty, MSRP | ใช้เมื่อคำถามเริ่มจาก SKU/product |
| `vw_policy_version_enriched` | policy version, effective date, end date | ใช้เมื่อยังต้องดู policy เป็นช่วง ไม่ใช่รายวัน |

## Lookup-Expanded Views

| View | ใช้เมื่อคำถามถามเรื่อง | ประโยชน์ต่อ LLM |
| --- | --- | --- |
| `vw_policy_daily_lookup` | policy ที่มีผล ณ วันที่ X เช่น return window วันที่ 2024-12-15 | ไม่ต้องเขียน `effective_date <= X AND (end_date IS NULL OR end_date >= X)` เอง |
| `vw_campaign_daily_lookup` | campaign/promo ที่ active ในวันที่ X | ไม่ต้องแปลง `start_timestamp/end_timestamp` เอง |
| `vw_vendor_contract_daily_lookup` | contract version ของ vendor ที่ active ณ วันที่ X | ใช้กับ vendor contract/as-of questions ได้ง่าย |

ตัวอย่าง date-aware policy:

```sql
SELECT *
FROM vw_policy_daily_lookup
WHERE query_date = DATE '2024-12-15'
  AND policy_variable = 'return_window_days';
```

## Answer-Ready Summary Views

| View | ใช้เมื่อคำถามถามเรื่อง | ประโยชน์ต่อ LLM |
| --- | --- | --- |
| `vw_promo_txn_dedup_flags` | หา redemption ซ้ำต่อ `campaign_id + txn_id`, duplicate rows, cross-channel duplicate | เตรียม flag สำหรับคำถาม hard/extremely hard เรื่อง phantom redemption |
| `vw_promo_campaign_summary` | สรุป campaign ทั้งก้อน: redemptions, unique txns, duplicate, discount, net revenue, ROI | คำถามเปรียบเทียบ campaign/ROI ตอบจาก view เดียว |
| `vw_vendor_invoice_summary` | invoice ID ซ้ำ, vendor payment count, total paid, bank txn ids | ลดงาน duplicate invoice investigation |
| `vw_refund_sku_summary` | SKU ที่ refund/return มากสุด ตามจำนวนหรือยอดเงิน | ตอบ top refunded SKU ได้โดยไม่ join return/refund/product เอง |
| `vw_product_return_rate_summary` | return rate / refund rate ต่อ SKU เทียบกับยอดขาย | ใช้กับคำถาม defect/quality/return-rate |
| `vw_branch_daily_sales_summary` | daily sales ราย branch, holiday, paid/B2B count | ใช้กับ branch-day operations และ trend |
| `vw_sku_daily_sales_summary` | daily sales ราย SKU, units, discount, care plus | ใช้กับ product-day trend หรือ campaign cohort |
| `vw_customer_value_summary` | customer value, total sales, refunds, CS interactions, loyalty points | ใช้กับคำถามลูกค้าคนไหนซื้อเยอะ/คืนเยอะ/value สูง |
| `vw_employee_activity_summary` | employee performance/activity: sales, approvals, vendor signing, payroll, CS | ใช้กับคำถามพนักงาน/approver/signer |
| `vw_bank_monthly_account_summary` | monthly bank-account inflow/outflow/net/balance | ใช้กับ bank statement/reconciliation รายเดือน |
| `vw_sales_payment_collection` | due date, received date, late/uncollected sales, B2B receivables | ใช้กับ AR/payment collection questions |
| `vw_sales_bank_reconciliation` | compare sales net total กับ bank settlement amount | ใช้กับ reconciliation ระหว่าง sale กับ bank txn |
| `vw_signing_authority_rules` | signing authority ladder, amount ceiling, cosigner requirement | ใช้กับคำถาม approval authority และ policy compliance |

## Query Examples

### SKU ที่ refund มากที่สุดตามจำนวนครั้ง

```sql
SELECT
  sku_id,
  brand_family,
  category,
  subcategory,
  refund_count,
  refund_amount_thb
FROM vw_refund_sku_summary
ORDER BY refund_count DESC, refund_amount_thb DESC
LIMIT 10;
```

### SKU ที่ refund มากที่สุดตามยอดเงิน

```sql
SELECT
  sku_id,
  brand_family,
  category,
  subcategory,
  refund_count,
  refund_amount_thb
FROM vw_refund_sku_summary
ORDER BY refund_amount_thb DESC
LIMIT 10;
```

### Campaign summary / ROI

```sql
SELECT
  campaign_id,
  description_en,
  redemption_rows,
  unique_txns,
  cross_channel_duplicate_txn_count,
  sales_discount_total_thb,
  sales_net_total_thb,
  roi_net_revenue_to_sales_discount
FROM vw_promo_campaign_summary
ORDER BY campaign_id;
```

### หา phantom redemption ต่อ campaign

```sql
SELECT
  campaign_id,
  txn_id,
  redemption_rows,
  distinct_redemption_channels,
  redemption_channels,
  redemption_discount_thb,
  sales_discount_total_thb,
  sales_net_total_thb
FROM vw_promo_txn_dedup_flags
WHERE campaign_id = 'SF-LAUNCH-2568'
  AND has_cross_channel_redemption
ORDER BY redemption_rows DESC, txn_id;
```

### Vendor invoice ID ซ้ำ

```sql
SELECT
  vendor_id,
  vendor_name_en,
  vendor_invoice_id,
  payment_rows,
  total_paid_amount_thb,
  payment_ids,
  bank_txn_ids
FROM vw_vendor_invoice_summary
WHERE is_duplicate_invoice_id
ORDER BY payment_rows DESC, total_paid_amount_thb DESC;
```

### Branch daily sales

```sql
SELECT
  business_event_date,
  branch_code,
  branch_name_en,
  sales_txns,
  net_total_thb,
  b2b_txns,
  paid_txns
FROM vw_branch_daily_sales_summary
WHERE business_event_date BETWEEN DATE '2025-07-01' AND DATE '2025-07-31'
ORDER BY business_event_date, branch_code;
```

### Product return rate

```sql
SELECT
  sku_id,
  brand_family,
  category,
  units_sold,
  return_count,
  refund_amount_thb,
  return_rate_by_line_item,
  refund_amount_rate
FROM vw_product_return_rate_summary
ORDER BY return_rate_by_line_item DESC NULLS LAST
LIMIT 20;
```

### Payment collection / late B2B invoices

```sql
SELECT
  txn_id,
  customer_id,
  net_total_thb,
  payment_due_date,
  payment_received_date,
  days_after_due,
  is_paid_late,
  is_uncollected_with_due_date
FROM vw_sales_payment_collection
WHERE is_b2b
  AND (is_paid_late OR is_uncollected_with_due_date)
ORDER BY payment_due_date;
```

### Sales-to-bank settlement diff

```sql
SELECT
  txn_id,
  net_total_thb,
  settlement_bank_txn_id,
  bank_amount_thb,
  bank_minus_sales_net_thb
FROM vw_sales_bank_reconciliation
WHERE abs_bank_sales_diff_thb > 0.01
ORDER BY abs_bank_sales_diff_thb DESC;
```

## Routing Rules for LLM

ใช้ heuristic นี้เพื่อลดการเดา:

| ถ้าคำถามมีคำว่า | เริ่มที่ view |
| --- | --- |
| sales, ยอดขาย, transaction, channel, B2B, payment status | `vw_sales_enriched` |
| SKU, product, category, brand, units sold | `vw_sales_line_enriched` หรือ `vw_sku_daily_sales_summary` |
| refund, return, คืนสินค้า, คืนเงิน | `vw_returns_refunds_enriched`, `vw_refund_sku_summary`, `vw_product_return_rate_summary` |
| campaign, promo, redemption, 11.11, ROI, phantom | `vw_promo_campaign_summary`, `vw_promo_txn_dedup_flags` |
| invoice, vendor payment, PayWise, duplicate invoice | `vw_vendor_invoice_summary`, `vw_vendor_payment_enriched` |
| bank, statement, balance, reconciliation | `vw_bank_transaction_enriched`, `vw_bank_monthly_account_summary`, `vw_sales_bank_reconciliation` |
| policy, effective date, ณ วันที่, as-of | `vw_policy_daily_lookup` |
| contract, vendor version, amendment | `vw_vendor_contract_daily_lookup` |
| approver, signer, cosigner, authority | `vw_signing_authority_rules`, `vw_employee_activity_summary` |
| customer value, loyalty, CS | `vw_customer_value_summary`, `vw_loyalty_ledger_enriched`, `vw_customer_service_enriched` |
| stock, inventory, closing units | `vw_inventory_movement_enriched`, `vw_inventory_snapshot_enriched` |

## Notes

- Views เหล่านี้ไม่ได้ลบ raw tables; ถ้าคำถามระบุ source table ตรง ๆ เช่น `FACT_PROMO_REDEMPTION` ให้ใช้ raw table หรือ view ที่ base มาจาก table นั้นตามความเหมาะสม
- `business_event_date` ใช้ตอบ “เหตุการณ์เกิดวันไหน”; `posting_date` ใช้ตอบ “ลงบัญชี/ธนาคารวันไหน”
- `fiscal_year` เป็นปี พ.ศ. เช่น `2567`, `2568`; ถ้า UI แสดง `2,567` เป็นแค่ numeric formatting
- คำถาม private/hard มักต้องใช้ views เหล่านี้ร่วมกับ docs/logs/rendered artifacts ต่อไป
