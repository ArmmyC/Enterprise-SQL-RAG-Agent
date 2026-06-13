# FahMai Table Relationships

Source: CSV files in `fah-mai-the-finale-enterprise-data-agentic-showdown/tables`.

The source CSVs do not declare database constraints, so the relationships below are inferred from column names and validated against the data.

## Candidate Primary Keys

| Table | Rows | Candidate primary key | Other unique not-null candidates |
| --- | ---: | --- | --- |
| `DIM_BANK_ACCOUNT` | 14 | `account_id` | `account_number` |
| `DIM_BRANCH` | 11 | `branch_code` | `name_th`, `name_en` |
| `dim_care_plus_sku_tier` | 2 | `tier_row_id` | `sku_id`, `care_plus_price_thb`, `coverage_months`, `description_th` |
| `DIM_CUSTOMER` | 30,000 | `customer_id` | `email`, `phone` |
| `DIM_DATE` | 731 | `date_iso` | `date_be_string` |
| `DIM_DEPARTMENT` | 9 | `dept_code` | `dept_name_th`, `dept_name_en` |
| `DIM_EMPLOYEE` | 600 | `employee_id` | `first_name_th`, `first_name_en`, `email` |
| `DIM_POLICY_VERSION` | 12 | `policy_version_id` | - |
| `DIM_POSITION_LEVEL` | 6 | `position_level_code` | `rank`, `default_signing_authority_thb` |
| `DIM_PRODUCT` | 110 | `sku_id` | - |
| `dim_product_recall_history` | 3 | `history_id` | `status`, `transition_date` |
| `DIM_PROMO_CAMPAIGN` | 7 | `campaign_id` | `start_timestamp`, `end_timestamp`, `description_th`, `description_en` |
| `dim_promo_mechanic` | 8 | `promo_mechanic_id` | - |
| `dim_signing_authority_ladder` | 7 | `ladder_row_id` | - |
| `DIM_VENDOR` | 6 | `vendor_id` | `name_th`, `name_en` |
| `DIM_VENDOR_CONTRACT_VERSION` | 22 | `contract_version_id` | `contract_pdf_filename` |
| `FACT_BANK_TRANSACTION` | 65,334 | `bank_txn_id` | - |
| `FACT_CS_INTERACTION` | 14,368 | `cs_interaction_id` | - |
| `FACT_INVENTORY_MONTHLY_SNAPSHOT` | 26,220 | `snapshot_id` | - |
| `FACT_INVENTORY_MOVEMENT` | 310,827 | `movement_id` | - |
| `FACT_LOYALTY_LEDGER` | 118,857 | `ledger_id` | - |
| `FACT_PAYROLL` | 14,400 | `payroll_id` | `bank_txn_id` |
| `FACT_PROMO_REDEMPTION` | 1,583 | `redemption_id` | - |
| `FACT_REFUND_PAID` | 7,134 | `refund_id` | `bank_txn_id` |
| `FACT_RETURN` | 7,144 | `return_id` | - |
| `FACT_SALES` | 117,105 | `txn_id` | - |
| `FACT_SALES_LINE_ITEM` | 309,129 | `line_item_id` | - |
| `FACT_SHIPPING` | 23,182 | `shipping_id` | `txn_id` |
| `FACT_VENDOR_PAYMENT` | 809 | `payment_id` | `bank_txn_id` |
| `FACT_WARRANTY_CLAIM` | 3,973 | `claim_id` | - |
| `T2_DOC_INVENTORY` | 81 | `doc_id` | `body_source` |

## Inferred Foreign Keys

| Child table.column | Parent table.column | Kind | Non-null rows | Match | Missing | Notes |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `DIM_BANK_ACCOUNT.associated_branch_code` | `DIM_BRANCH.branch_code` | FK | 11 | 11 (100.0%) | 0 | - |
| `DIM_CUSTOMER.account_manager_id` | `DIM_EMPLOYEE.employee_id` | FK | 300 | 300 (100.0%) | 0 | B2B account manager; nullable. |
| `DIM_EMPLOYEE.branch_code` | `DIM_BRANCH.branch_code` | FK | 600 | 600 (100.0%) | 0 | - |
| `DIM_EMPLOYEE.dept_code` | `DIM_DEPARTMENT.dept_code` | FK | 600 | 600 (100.0%) | 0 | - |
| `DIM_EMPLOYEE.position_level` | `DIM_POSITION_LEVEL.position_level_code` | FK | 600 | 600 (100.0%) | 0 | - |
| `DIM_EMPLOYEE.reports_to_employee_id` | `DIM_EMPLOYEE.employee_id` | FK | 598 | 598 (100.0%) | 0 | Self-reference; nullable for top-level staff. |
| `DIM_PRODUCT.dept_code` | `DIM_DEPARTMENT.dept_code` | FK | 1 | 1 (100.0%) | 0 | - |
| `DIM_PRODUCT.vendor_id` | `DIM_VENDOR.vendor_id` | FK | 50 | 50 (100.0%) | 0 | Nullable for in-house SKUs. |
| `DIM_PRODUCT.launch_date` | `DIM_DATE.date_iso` | Date FK | 110 | 4 (3.6%) | 106 | Some launches are before DIM_DATE coverage, e.g. 2023-01-01. |
| `DIM_PRODUCT.end_of_life_date` | `DIM_DATE.date_iso` | Date FK | 0 | 0 (100.0%) | 0 | Nullable. |
| `DIM_POLICY_VERSION.effective_date` | `DIM_DATE.date_iso` | Date FK | 12 | 12 (100.0%) | 0 | - |
| `DIM_POLICY_VERSION.end_date` | `DIM_DATE.date_iso` | Date FK | 5 | 5 (100.0%) | 0 | Nullable/open-ended. |
| `DIM_VENDOR.start_date` | `DIM_DATE.date_iso` | Date FK | 6 | 1 (16.7%) | 5 | Some vendor start dates are before DIM_DATE coverage, e.g. 2023-01-01. |
| `DIM_VENDOR.end_date` | `DIM_DATE.date_iso` | Date FK | 1 | 1 (100.0%) | 0 | Nullable/open-ended. |
| `DIM_VENDOR_CONTRACT_VERSION.vendor_id` | `DIM_VENDOR.vendor_id` | FK | 22 | 22 (100.0%) | 0 | - |
| `DIM_VENDOR_CONTRACT_VERSION.effective_date` | `DIM_DATE.date_iso` | Date FK | 22 | 22 (100.0%) | 0 | - |
| `DIM_VENDOR_CONTRACT_VERSION.end_date` | `DIM_DATE.date_iso` | Date FK | 17 | 17 (100.0%) | 0 | Nullable/open-ended. |
| `DIM_PROMO_CAMPAIGN.start_timestamp` | `DIM_DATE.date_iso` | Date-like FK | 7 | 7 (100.0%) | 0 | Timestamp; date part references DIM_DATE. |
| `DIM_PROMO_CAMPAIGN.end_timestamp` | `DIM_DATE.date_iso` | Date-like FK | 7 | 7 (100.0%) | 0 | Timestamp; date part references DIM_DATE. |
| `dim_care_plus_sku_tier.policy_version_id` | `DIM_POLICY_VERSION.policy_version_id` | FK | 2 | 2 (100.0%) | 0 | - |
| `dim_care_plus_sku_tier.sku_id` | `DIM_PRODUCT.sku_id` | FK | 2 | 2 (100.0%) | 0 | Nullable/scope row may apply by category instead of one SKU. |
| `dim_product_recall_history.sku_id` | `DIM_PRODUCT.sku_id` | FK | 3 | 3 (100.0%) | 0 | - |
| `dim_product_recall_history.transition_date` | `DIM_DATE.date_iso` | Date FK | 3 | 3 (100.0%) | 0 | - |
| `dim_promo_mechanic.campaign_id` | `DIM_PROMO_CAMPAIGN.campaign_id` | FK | 8 | 8 (100.0%) | 0 | - |
| `dim_signing_authority_ladder.policy_version_id` | `DIM_POLICY_VERSION.policy_version_id` | FK | 7 | 7 (100.0%) | 0 | - |
| `dim_signing_authority_ladder.position_level_code` | `DIM_POSITION_LEVEL.position_level_code` | FK | 7 | 7 (100.0%) | 0 | - |
| `dim_signing_authority_ladder.dept_code` | `DIM_DEPARTMENT.dept_code` | FK | 1 | 1 (100.0%) | 0 | - |
| `dim_signing_authority_ladder.co_signer_min_position_level_code` | `DIM_POSITION_LEVEL.position_level_code` | FK | 2 | 2 (100.0%) | 0 | Nullable when no co-signer requirement. |
| `FACT_BANK_TRANSACTION.account_id` | `DIM_BANK_ACCOUNT.account_id` | FK | 65,334 | 65,334 (100.0%) | 0 | - |
| `FACT_CS_INTERACTION.customer_id` | `DIM_CUSTOMER.customer_id` | FK | 14,368 | 14,368 (100.0%) | 0 | - |
| `FACT_CS_INTERACTION.employee_id` | `DIM_EMPLOYEE.employee_id` | FK | 14,368 | 14,368 (100.0%) | 0 | - |
| `FACT_CS_INTERACTION.branch_code` | `DIM_BRANCH.branch_code` | FK | 14,368 | 14,368 (100.0%) | 0 | - |
| `FACT_CS_INTERACTION.related_refund_id` | `FACT_REFUND_PAID.refund_id` | FK | 7,080 | 7,080 (100.0%) | 0 | Nullable. |
| `FACT_CS_INTERACTION.related_warranty_claim_id` | `FACT_WARRANTY_CLAIM.claim_id` | FK | 3,913 | 3,913 (100.0%) | 0 | Nullable. |
| `FACT_INVENTORY_MONTHLY_SNAPSHOT.sku_id` | `DIM_PRODUCT.sku_id` | FK | 26,220 | 26,220 (100.0%) | 0 | - |
| `FACT_INVENTORY_MONTHLY_SNAPSHOT.branch_code` | `DIM_BRANCH.branch_code` | FK | 26,220 | 26,220 (100.0%) | 0 | - |
| `FACT_INVENTORY_MONTHLY_SNAPSHOT.month_end_date` | `DIM_DATE.date_iso` | Date FK | 26,220 | 26,220 (100.0%) | 0 | - |
| `FACT_INVENTORY_MOVEMENT.sku_id` | `DIM_PRODUCT.sku_id` | FK | 310,827 | 310,827 (100.0%) | 0 | - |
| `FACT_INVENTORY_MOVEMENT.branch_code` | `DIM_BRANCH.branch_code` | FK | 310,827 | 310,827 (100.0%) | 0 | - |
| `FACT_LOYALTY_LEDGER.customer_id` | `DIM_CUSTOMER.customer_id` | FK | 118,857 | 118,857 (100.0%) | 0 | - |
| `FACT_LOYALTY_LEDGER.txn_id` | `FACT_SALES.txn_id` | FK | 75,357 | 75,357 (100.0%) | 0 | Nullable for non-sales loyalty events. |
| `FACT_PAYROLL.employee_id` | `DIM_EMPLOYEE.employee_id` | FK | 14,400 | 14,400 (100.0%) | 0 | - |
| `FACT_PAYROLL.bank_txn_id` | `FACT_BANK_TRANSACTION.bank_txn_id` | FK | 14,400 | 14,400 (100.0%) | 0 | - |
| `FACT_PAYROLL.pay_period_start` | `DIM_DATE.date_iso` | Date FK | 14,400 | 14,400 (100.0%) | 0 | - |
| `FACT_PAYROLL.pay_period_end` | `DIM_DATE.date_iso` | Date FK | 14,400 | 14,400 (100.0%) | 0 | - |
| `FACT_PROMO_REDEMPTION.txn_id` | `FACT_SALES.txn_id` | FK | 1,583 | 1,583 (100.0%) | 0 | - |
| `FACT_PROMO_REDEMPTION.customer_id` | `DIM_CUSTOMER.customer_id` | FK | 1,583 | 1,583 (100.0%) | 0 | - |
| `FACT_PROMO_REDEMPTION.campaign_id` | `DIM_PROMO_CAMPAIGN.campaign_id` | FK | 1,583 | 1,583 (100.0%) | 0 | - |
| `FACT_REFUND_PAID.return_id` | `FACT_RETURN.return_id` | FK | 7,116 | 7,116 (100.0%) | 0 | - |
| `FACT_REFUND_PAID.cs_interaction_id` | `FACT_CS_INTERACTION.cs_interaction_id` | FK | 0 | 0 (100.0%) | 0 | - |
| `FACT_REFUND_PAID.customer_id` | `DIM_CUSTOMER.customer_id` | FK | 7,134 | 7,134 (100.0%) | 0 | - |
| `FACT_REFUND_PAID.approver_employee_id` | `DIM_EMPLOYEE.employee_id` | FK | 7,134 | 7,134 (100.0%) | 0 | - |
| `FACT_REFUND_PAID.cosig_employee_id` | `DIM_EMPLOYEE.employee_id` | FK | 0 | 0 (100.0%) | 0 | Nullable. |
| `FACT_REFUND_PAID.bank_txn_id` | `FACT_BANK_TRANSACTION.bank_txn_id` | FK | 7,134 | 7,134 (100.0%) | 0 | - |
| `FACT_REFUND_PAID.request_date` | `DIM_DATE.date_iso` | Date FK | 7,134 | 7,134 (100.0%) | 0 | - |
| `FACT_RETURN.original_txn_id` | `FACT_SALES.txn_id` | FK | 7,144 | 7,144 (100.0%) | 0 | - |
| `FACT_RETURN.line_item_id` | `FACT_SALES_LINE_ITEM.line_item_id` | FK | 7,080 | 7,080 (100.0%) | 0 | - |
| `FACT_RETURN.sku_id` | `DIM_PRODUCT.sku_id` | FK | 7,144 | 7,144 (100.0%) | 0 | - |
| `FACT_RETURN.branch_code` | `DIM_BRANCH.branch_code` | FK | 7,144 | 7,144 (100.0%) | 0 | - |
| `FACT_RETURN.customer_id` | `DIM_CUSTOMER.customer_id` | FK | 7,144 | 7,144 (100.0%) | 0 | - |
| `FACT_RETURN.approved_by_employee_id` | `DIM_EMPLOYEE.employee_id` | FK | 7,144 | 7,144 (100.0%) | 0 | - |
| `FACT_SALES.branch_code` | `DIM_BRANCH.branch_code` | FK | 117,105 | 117,105 (100.0%) | 0 | - |
| `FACT_SALES.customer_id` | `DIM_CUSTOMER.customer_id` | FK | 17,912 | 17,912 (100.0%) | 0 | - |
| `FACT_SALES.employee_id` | `DIM_EMPLOYEE.employee_id` | FK | 3,693 | 3,693 (100.0%) | 0 | - |
| `FACT_SALES.promo_campaign_id` | `DIM_PROMO_CAMPAIGN.campaign_id` | FK | 5,568 | 5,568 (100.0%) | 0 | Nullable when no promo. |
| `FACT_SALES.settlement_bank_txn_id` | `FACT_BANK_TRANSACTION.bank_txn_id` | FK | 13,313 | 13,313 (100.0%) | 0 | Nullable/unsettled or non-bank payments. |
| `FACT_SALES.payment_due_date` | `DIM_DATE.date_iso` | Date FK | 14,220 | 13,331 (93.7%) | 889 | Nullable; some B2B due dates fall after DIM_DATE coverage, into 2026. |
| `FACT_SALES.payment_received_date` | `DIM_DATE.date_iso` | Date FK | 13,813 | 13,813 (100.0%) | 0 | Nullable. |
| `FACT_SALES_LINE_ITEM.txn_id` | `FACT_SALES.txn_id` | FK | 309,129 | 309,129 (100.0%) | 0 | - |
| `FACT_SALES_LINE_ITEM.sku_id` | `DIM_PRODUCT.sku_id` | FK | 309,129 | 309,129 (100.0%) | 0 | - |
| `FACT_SHIPPING.txn_id` | `FACT_SALES.txn_id` | FK | 23,182 | 23,182 (100.0%) | 0 | - |
| `FACT_SHIPPING.vendor_id` | `DIM_VENDOR.vendor_id` | FK | 23,182 | 23,182 (100.0%) | 0 | - |
| `FACT_SHIPPING.origin_branch_code` | `DIM_BRANCH.branch_code` | FK | 23,182 | 23,182 (100.0%) | 0 | - |
| `FACT_VENDOR_PAYMENT.vendor_id` | `DIM_VENDOR.vendor_id` | FK | 809 | 809 (100.0%) | 0 | - |
| `FACT_VENDOR_PAYMENT.vendor_contract_version_id` | `DIM_VENDOR_CONTRACT_VERSION.contract_version_id` | FK | 809 | 809 (100.0%) | 0 | - |
| `FACT_VENDOR_PAYMENT.signing_employee_id` | `DIM_EMPLOYEE.employee_id` | FK | 809 | 809 (100.0%) | 0 | - |
| `FACT_VENDOR_PAYMENT.cosig_employee_id` | `DIM_EMPLOYEE.employee_id` | FK | 1 | 1 (100.0%) | 0 | Nullable. |
| `FACT_VENDOR_PAYMENT.bank_txn_id` | `FACT_BANK_TRANSACTION.bank_txn_id` | FK | 809 | 809 (100.0%) | 0 | - |
| `FACT_VENDOR_PAYMENT.request_date` | `DIM_DATE.date_iso` | Date FK | 809 | 809 (100.0%) | 0 | - |
| `FACT_VENDOR_PAYMENT.invoice_period_start` | `DIM_DATE.date_iso` | Date FK | 809 | 776 (95.9%) | 33 | Some periods start before DIM_DATE coverage, e.g. 2023-12-01. |
| `FACT_VENDOR_PAYMENT.invoice_period_end` | `DIM_DATE.date_iso` | Date FK | 809 | 776 (95.9%) | 33 | Some periods end before DIM_DATE coverage, e.g. 2023-12-31. |
| `FACT_WARRANTY_CLAIM.customer_id` | `DIM_CUSTOMER.customer_id` | FK | 3,973 | 3,973 (100.0%) | 0 | - |
| `FACT_WARRANTY_CLAIM.sku_id` | `DIM_PRODUCT.sku_id` | FK | 3,973 | 3,973 (100.0%) | 0 | - |
| `FACT_WARRANTY_CLAIM.original_txn_id` | `FACT_SALES.txn_id` | FK | 3,913 | 3,913 (100.0%) | 0 | - |

## Bitemporal Columns

Most fact tables share four date columns. These should usually be interpreted differently:

- `business_event_date`: when the business event happened
- `posting_date`: when it was posted/accounted
- `effective_date`: when the row/version became effective
- `as_of_date`: as-of extraction or reporting date

| Table | business_event_date | posting_date | effective_date | as_of_date |
| --- | --- | --- | --- | --- |
| `FACT_BANK_TRANSACTION` | yes | yes | yes | yes |
| `FACT_CS_INTERACTION` | yes | yes | yes | yes |
| `FACT_INVENTORY_MONTHLY_SNAPSHOT` | yes | yes | yes | yes |
| `FACT_INVENTORY_MOVEMENT` | yes | yes | yes | yes |
| `FACT_LOYALTY_LEDGER` | yes | yes | yes | yes |
| `FACT_PAYROLL` | yes | yes | yes | yes |
| `FACT_PROMO_REDEMPTION` | yes | yes | yes | yes |
| `FACT_REFUND_PAID` | yes | yes | yes | yes |
| `FACT_RETURN` | yes | yes | yes | yes |
| `FACT_SALES` | yes | yes | yes | yes |
| `FACT_SALES_LINE_ITEM` | yes | yes | yes | yes |
| `FACT_SHIPPING` | yes | yes | yes | yes |
| `FACT_VENDOR_PAYMENT` | yes | yes | yes | yes |
| `FACT_WARRANTY_CLAIM` | yes | yes | yes | yes |

## Polymorphic References

`FACT_BANK_TRANSACTION.related_entity_table` plus `related_entity_id` is a polymorphic reference rather than a single foreign key.

| related_entity_table | Rows | Distinct IDs |
| --- | ---: | ---: |
| `FACT_SALES_DEPOSIT_BATCH` | 28,279 | 28,279 |
| `FACT_PAYROLL` | 14,400 | 14,400 |
| `FACT_SALES` | 13,313 | 13,313 |
| `FACT_REFUND_PAID` | 7,134 | 7,134 |
| `FACT_LOYALTY_LEDGER` | 1,255 | 1,255 |
| `FACT_VENDOR_PAYMENT` | 809 | 809 |

`T2_DOC_INVENTORY.source_table` plus `source_pk` is another polymorphic reference to the source table behind a generated/rendered document.

| source_table | Rows | Distinct source_pk |
| --- | ---: | ---: |
| `DIM_POLICY_VERSION` | 12 | 12 |
| `DIM_VENDOR_CONTRACT_VERSION` | 22 | 22 |
| `T2_DOC_INVENTORY` | 47 | 47 |

## Practical Query Hints

- Use `DIM_DATE.date_iso` for calendar joins; fiscal year values are Thai Buddhist years (`2567`, `2568`).
- Use `FACT_SALES.txn_id` as the central sales transaction key.
- Use `FACT_SALES_LINE_ITEM.line_item_id` for item-level questions and join back to `FACT_SALES.txn_id`.
- Use `FACT_BANK_TRANSACTION.bank_txn_id` for bank/accounting reconciliation.
- Treat nullable relationship columns carefully; many optional operational links are intentionally blank.
