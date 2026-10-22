# 金蝶云星空查询手册

### 1. 核心规则

1. **固定工具优先**：工具描述以“金蝶固定工具”开头并写明触发 FormId 的 MCP，就是固定查询工具。用户意图命中描述中的业务名，或 `k3_search_forms` 候选出现描述中的 FormId 时，直接调用该工具，不要走探测流程。
2. **未固定才探测**：没有固定工具覆盖时，先 `k3_search_forms` 搜索静态目录；返回 `NO_CANDIDATES` 时，按 2. 通用查询流程的清单用 `k3_describe_form` 验证标准 FormId。
3. **不猜 FormId、不猜字段名**：未固定领域的字段 Key 必须先经 `k3_describe_form(formId)` 确认；本手册列出的字段是实测参考，账套差异仍以实测返回为准。
4. **固定工具一律全量查询**：所有金蝶固定工具默认必须使用全字段/全分录视图（如 `view=full`）；仅当用户明确要求精简视图时才允许传精简参数（如 `view=header`），且确认选项必须包含全字段明细选项，不得预写具体列名诱导裁剪。
5. **原样使用返回数据**：回答和交付内容一律以工具返回的 `fields` 为列集、`data` 为行集原样使用；禁止手写常量、人工转抄或裁剪字段。用户需要精简列时，先向用户确认保留哪些列。
6. **只走金蝶查询工具**：查询金蝶账套数据时，优先调金蝶固定工具，否则调 `k3_query_data` 或 `k3_query_report`。本手册只管金蝶；依云、水趣等非金蝶系统走 `linko_ent`，与本手册无关。
7. **连续失败 3 次停止**：对同一业务目标，固定工具、`k3_query_data`、`k3_query_report` 连续失败 3 次必须停止，按 9. 连续失败停止规则报告阻塞点。
8. **数据含义不确定必须问，禁止推测**：工具返回的字段、值、枚举、状态码，若本手册或工具 description 没有明确解释，不得靠常识解读给用户；必须说明"该字段含义需要确认"或向用户/管理员确认后再解读。违反即事故。
9. **禁止把空结果当作结论**：查到 0 行时，只能如实告知"在当前表单/条件下没有查到记录"，不得推测"这个客户不存在"或"没有业务发生"。可能原因是系统选错、FormId 选错、过滤条件过严，须先排除这些原因或让用户确认。


### 2. 通用查询流程

```text
Step 1  定位表单   k3_search_forms(intentFragment="应收单")
                   → 命中取 formId；NO_CANDIDATES 时用 k3_describe_form 验证标准 FormId
Step 2  确认字段   k3_describe_form(formId) → 真实字段 Key 列表
Step 3  查询数据   k3_query_data(formId, fieldKeys, filterString, order, startRow, limit)
                   或账表类：k3_query_report(formId, dataJson)
```

`k3_describe_form` 技巧：

- 返回 `verified=true` 表示当前账套确实存在该 FormId；返回字段列表是本次可用的事实。
- 探测成功后服务端自动缓存（30 天 TTL），后续 `k3_search_forms` 相同关键词即可命中；不要反复换词试探。
- 探测失败表示该 FormId 在当前账套不存在，换候选或向用户澄清。
- 字段列表最多返回 500 项；达到上限说明不是完整 Schema，只取需要的字段。
- 它只验证存在性，不验证业务语义；多个候选时须向用户确认。

`k3_suggest_params` 使用时机：

- 同一 FormId 被多次查询后，可读取之前确认过的参数建议（账簿、期间、状态等）。
- 建议可能过期，与本次用户范围不一致时必须再次澄清，不能静默复用。

### 3. 实测字段知识

以下 FormId 与字段在本账套经 `QueryBusinessInfo` 实测确认；账套版本差异仍以 `k3_describe_form` 实测为准。静态目录其余表单直接用 `k3_search_forms` 搜索，本手册不重复目录清单。

#### 3.0 财务表通用规律

1. **会计期间用 `FYEAR` + `FPERIOD`**（不是日期）：`FPERIOD` 是 1..12 的期间序号；业务日期才是 `FDate`。
2. **维度**：账簿 `FAccountBookID`、组织 `FOrgId`/`FSETTLEORGID`、币别 `FCURRENCYID`。
3. **主键不一定是 `FID`**：凭证主键是 `FVOUCHERID`；科目余额表/账表类无 `FID` → 必须显式指定排序字段，否则报“列名 'FID' 无效”。
4. **分录内码统一写法**：`<分录Key>_FEntryId`，如 `FEntity_FEntryId`。
5. **基础资料取值**：名称 `某字段.FName`、编码 `某字段.FNumber`（如 `FAccountID.FNumber`）。
6. **FormId 大小写不敏感**：`GL_Voucher` 与 `GL_VOUCHER` 等价，以 BOS 对象标识为准。

#### 3.1 总账（GL）

| FormId | 名称 | 表头常用字段 | 分录 Key / 字段 |
| --- | --- | --- | --- |
| `GL_Voucher_New` | 新版总账凭证；由金蝶固定工具 `k3_query_vouchers` 查询，默认 `view=full` 返回 ERP 全字段分录展开视图，仅用户明确要求凭证级精简清单时才传 `view=header`（15 字段） | 账簿 `FAccountBookID.FNumber/FName`、年度 `FYear`、期间 `FPeriod`、凭证字 `FVOUCHERGROUPID.FName`、组内凭证号 `FVOUCHERGROUPNO`、凭证日期 `FDATE`、头金额 `FDEBITTOTAL/FCREDITTOTAL`、状态 `FDocumentStatus/FPOSTED/FINVALID` | 分录字段：摘要 `FEXPLANATION`、科目 `FACCOUNTID.FNumber/FName`、借贷方向 `FDC`、金额 `FAMOUNTFOR/FAMOUNT/FDEBIT/FCREDIT`。返回值 `voucherCount` 是凭证张数（按 FBillNo 去重），`entryCount` 是分录行数；`hasMore=true` 时按分录行翻页 |
| `GL_Voucher` | 旧版凭证。除非用户明确要求旧版，或固定工具连续失败且 `k3_describe_form` 已确认可用，否则不要首选 | `FVOUCHERID`（主键，非 FID）、`FBillNo`、`FDate`、`FYEAR`、`FPERIOD`、`FAccountBookID.FNumber`、`FVOUCHERGROUPID.FNumber`、`FDocumentStatus` | `FEntity`：`FACCOUNTID.FNumber`、`FDEBIT`、`FCREDIT`、`FAMOUNTFOR`、`FCURRENCYID.FNumber`、`FEXPLANATION`、`FDetailID__FFLEX*`（核算维度） |
| `GL_Balance` | 科目余额表 | `FACCOUNTBOOKID`、`FAccountID`、`FCurrencyID`、`FYear`、`FPeriod`、`FBeginBalance`（期初）、`FEndBalance`（期末） | 无分录 |
| `BD_Account` | 会计科目 | `FNumber`（科目编码）、`FName`、`FGROUPID`（科目类别）、`FDC`（余额方向）、`FITEMDETAILID`（核算维度）、`FParentID` | `FAcctCy`：`FCurrencyID`；`FEntity`：`FFlexItemPropertyID`；`FDistEntity`：`FDISTORGID` |
| `BD_AccountBook` | 账簿 | `FNumber`、`FName`、`FCURRENCYID`、`FACCTTABLEID`（科目表）、`FPeriodid`（会计日历）、`FYearAndPeriod`、`FAccountOrgID` | `FEntity`：`FYEAR`/`FADJUSTNAME` |
| `BD_VoucherGroup` | 凭证字 | `FName`、`FAcctTableId`（科目表） | `FAcctControl`：`FAcctNumber` |

注意事项：

- `GL_Balance` 字段命名随版本不同，以 `k3_describe_form("GL_Balance")` 实测为准，不要照抄 API 资料。
- 用户只说凭证/总账凭证时，默认查 `GL_Voucher_New` 并调用金蝶固定工具 `k3_query_vouchers`；不要把客户/供应商当作账簿。
- `GL_VoucherGroup`、`BD_VoucherType` **实测不存在**；凭证字用 `BD_VoucherGroup`。
- ERP 界面凭证号 = 凭证字 + 组内序号（如转-30）；`FBillNo` 是系统内部编号，不能拿去 ERP 搜。

#### 3.2 应收（AR）与应付（AP）

| FormId | 名称 | 表头常用字段（实测） | 分录 Key / 字段（实测） |
| --- | --- | --- | --- |
| `AR_Receivable` | 应收单 | `FDATE`、`FSETTLEORGID`、`FCURRENCYID`、`FBillTypeID`、`FCUSTOMERID`、`FSALEORGID`、`FENDDATE_H`（到期日）、`FPAYORGID` | `FEntityDetail`：`FMATERIALID`、`FPrice`、`FTaxPrice`、`FPriceQty`、`FEntryTaxRate`、`FTaxAmountFor_D`、`FAllAmountFor_D`；另有 `FEntityPlan`（收款计划）、`FsubHeadFinc`、`FARCOSTENTRY` |
| `AR_ReceiveBill` | 收款单 | `FDATE`、`FCURRENCYID`、`FBillTypeID`、`FCONTACTUNIT`（往来单位）、`FPAYUNIT`（付款单位）、`FSETTLECUR`、`FDOCUMENTSTATUS` | `FRECEIVEBILLENTRY`：`FSETTLETYPEID`、`FACCOUNTID`、`FPURPOSEID`、`FPrice`、`FQty`；另有 `FRECEIVEBILLSRCENTRY` |
| `AR_RefundBill` | 收款退款单 | `FDATE`、`FCURRENCYID`、`FBillTypeID`、`FCONTACTUNIT`、`FRECTUNIT`（收款单位）、`FSETTLEORGID` | `FREFUNDBILLENTRY`：`FSETTLETYPEID`、`FACCOUNTID`、`FPURPOSEID`、`FRecType`、`FRuZhangType` |
| `AP_Payable` | 应付单 | `FDATE`、`FSETTLEORGID`、`FCURRENCYID`、`FSUPPLIERID`、`FBillTypeID`、`FENDDATE_H`（到期日）、`FPAYORGID`、`FAllAmountFor` | `FEntityDetail`：`FMATERIALID`、`FPrice`、`FTaxPrice`、`FPriceQty`、`FEntryTaxRate`、`FTaxAmountFor_D`、`FAllAmountFor_D` |
| `AP_PayBill` | 付款单 | `FDATE`、`FCURRENCYID`、`FBillTypeID`、`FCONTACTUNIT`、`FRECTUNIT`（收款单位）、`FSETTLECUR` | `FPAYBILLENTRY`：`FSETTLETYPEID`、`FACCOUNTID`、`FPURPOSEID`、`FRuZhangType`、`FPrice`、`FQty`；另有 `FPAYBILLSRCENTRY` |
| `AP_RefundBill` | 付款退款单 | `FDATE`、`FCURRENCYID`、`FBillTypeID`、`FCONTACTUNIT`、`FPAYUNIT`（付款单位）、`FSETTLEORGID` | `FREFUNDBILLENTRY`：`FSETTLETYPEID`、`FACCOUNTID`、`FPURPOSEID`、`FPrice`、`FQty` |

`AR_Invoice`、`AP_Invoice`、`AR_RecBill` **实测不存在**；发票/核销类需求先向用户确认或走应收/应付单据本身。

#### 3.3 财务报表（账表，与单据不同）

| FormId | 名称 | 实测结果 |
| --- | --- | --- |
| `CR_Report` | 报表（加载数据） | 实测存在：`FNumber`、`FCycleID`、`FPolicyId`、`FCURRENCYID`、`FCurrUnitID`、`FOrgId`；分录 `FEntity`（取数账簿）、`FRptExtension`、`FRptReportInfo` |
| `GL_RPT_AccountBalance` / `GL_RPT_BalanceSheet` / `GL_RPT_ProfitStatement` | 科目余额表 / 资产负债表 / 利润表 | 实测“业务对象不存在”——报表不走 `QueryBusinessInfo`，须用 `k3_query_report`（GetSysReportData）查询 |
| `ar_acctagerpt_n` / `ap_acctagerpt_n` | 应收 / 应付账龄分析表（新） | 未实测，用前先 `k3_describe_form` 探测 |
| `ar_sumrpt_n` / `ap_sumrpt_n`；`ar_detailrpt_n` / `ap_detailrpt_n` | 应收 / 应付汇总表 / 明细表（新） | 同上，未实测 |

账表与单据的差别：

- 返回字段是动态生成的，`fieldKeys` 只能给最大候选集，最终结果集可能少于所选字段。
- 部分报表走 `GetSysReportData`（非 `ExecuteBillQuery`），必须用 `k3_query_report` 传 `formId` 和 `dataJson`。
- 零行不等于选错模型，也不证明选对模型；关键参数不明确时先向用户确认。

#### 3.4 固定资产 / 出纳

| FormId | 名称 | 表头重点字段（实测） |
| --- | --- | --- |
| `FA_Card` | 固定资产·资产卡片 | `FName`、`FAssetTypeID`、`FAssetOrgID`、`FAssetStatusID`、`FAlterModeID`、`FBeginUseDate`、`FASSETNATURE`、`FOWNERORGID`；分录 `FFinance`/`FCardDetail`（`FAssetNO`）/`FAllocation`/`FDEVICE`/`FInvoice` |
| `FA_AssetType` | 资产类别 | `FName`、`FAssetCodeRuleID`、`FCodeRuleID`、`FUnitId` |
| `CN_BankAcnt` | 银行账号 | `FNumber`（账号）、`FName`、`FBANKID`、`FInOrOut`、`FACCOUNTTYPE`、`FCURRENCYID` |
| `BD_Bank` | 银行 | `FName`、`FBankType`、`FBankDetail` |

#### 3.5 供应链补充

| FormId | 分录 Key | 重点字段 |
| --- | --- | --- |
| `PUR_PurchaseOrder` | `FPOOrderEntry` | `FMATERIALID`、`FQTY`、`FPRICE`、`FALLAMOUNT`、`FTAXPRICE`、`FEntryTaxRate`、`FDELIVERYDATE` |
| `SAL_SaleOrder` | `FSaleOrderEntry` | `FMATERIALID`、`FQTY`、`FPRICE`、`FALLAMOUNT`、`FTAXPRICE`、`FEntryTaxRate`、`FDELIVERYDATE` |
| `STK_Inventory` | 无分录（单行） | `FMATERIALID`、`FSTOCKID`、`FBaseQty`、`FLot`、`FBaseAmount` |

### 4. 凭证查询特殊规则

- 账簿名称转编码：用户提到账簿名称但未给编码时，先用 `BD_AccountBook` 把名称解析成 `FNumber`，再拼进 `FAccountBookID.FNumber='<编码>'` 过滤条件。
- 数量口径：`voucherCount` 是凭证张数（按 FBillNo 去重），`entryCount` 是分录行数；说“多少张凭证”用 `voucherCount`，说“多少条分录/明细行”用 `entryCount`。
- 翻页与汇总：`hasMore=true` 时先向用户说明数量规模；确需继续时按 `startRow=limit` 翻页（分页作用于分录行）。汇总总额优先使用返回结果中的单据头金额 `FDEBITTOTAL/FCREDITTOTAL`；确需按分录汇总时，先向用户确认数据规模或翻页策略，不要无上限拉全量分录。
- 语义等价：不要把“凭证/凭证分录”当成“总账余额/科目余额/报表”的替代结果；拿不到余额或报表时明确说明缺口。

### 5. 参数规范

#### k3_query_data

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| formId | string | 是 | 金蝶表单 ID，先用 `k3_search_forms` 或 `k3_describe_form` 确认 |
| fieldKeys | string | 是 | 逗号分隔的真实字段 Key，先用 `k3_describe_form` 核对 |
| filterString | string | 否 | 金蝶原生过滤表达式（非 SQL，不支持 GETDATE/CONVERT/DATEADD），如 `FDocumentStatus='C'` |
| order | string | 否 | 排序表达式，如 `FID desc` 或 `FAccountID.FNumber asc`；order 必须来自本次 fieldKeys 中的字段 |
| startRow | int | 否 | 分页起点，从 0 开始 |
| limit | int | 否 | 单页行数，最大 100 |

`order` 为空时系统按首个 FieldKey 升序降级；部分汇总表单没有 `FID`，禁止假设其存在。

#### k3_query_report

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| formId | string | 是 | 金蝶报表 FormId，如 `GL_RPT_AccountBalance` |
| dataJson | string | 是 | GetSysReportData 的结构化参数 JSON，含 Model（账簿）、FieldKeys、分页 |

dataJson 示例：

```json
{
  "Model": { "FACCTBOOKID": { "FNumber": "018" } },
  "FieldKeys": "FBALANCEID,FBALANCENAME,FBEGINDEBIT,FBEGINCREDIT,FDEBIT,FCREDIT,FENDDEBIT,FENDCREDIT",
  "FSTARTYEAR": 2026, "FSTARTPERIOD": 10, "FENDYEAR": 2026, "FENDPERIOD": 10,
  "FBALANCELEVEL": 1, "FSHOWFULLNAME": true,
  "StartRow": 0, "Limit": 100
}
```

`FBALANCELEVEL=0` 时配 `FSHOWFULLNAME=true`。

#### 租户与账套边界

当前 MCP 实例绑定一个客户租户及其金蝶账套，由部署配置决定，Agent 会话内**不支持**切换租户或账套。用户需要另一个租户的数据时，说明当前实例只接一个账套，需要部署对应实例或联系管理员。

### 6. 反模式

- 不要凭记忆猜字段名，报错后再换一个猜；用 `k3_describe_form` 一次拿全。
- 不要同一关键词反复 `k3_search_forms` 换词试探；`NO_CANDIDATES` 时直接 `k3_describe_form` 验证标准 FormId。
- 不要无过滤条件直接拉全表；先 `limit=5` 验证通道，再加精确条件。
- 不要一次并行猜多个 FormId；一次一步，按返回决定下一步。
- 不要把 `filterString` 当 SQL 写；用金蝶原生表达式和结构化参数。
- 无过滤条件时避免“不加/不要”式参数表述；只写正向过滤，如 `FNumber>='000'`。
- 不要手写常量、人工转抄数据或自行裁剪字段；导出脚本直接读取 MCP 返回的 `fields/data` 原样生成文件。

### 7. 信息安全（违反即严重事故）

- 禁止向用户展示 MCP 服务器 URL、端口、内部异常类名或 Spring 堆栈；只说“服务暂时不可用”或“调用受限”。
- 禁止把 `mcp.json` 配置内容、认证 token、环境变量值贴给用户；只说“连接配置需要检查”，具体配置由管理员处理。
- 禁止把底层错误原文（如 "Public MCP request context is missing"）转述给用户；只说“当前调用需要安全凭证，请检查 Agent 连接配置”。
- 禁止引导用户修改 `mcp.json` 或猜测协议字段；明确说“需要管理员在部署侧配置”。
- 查询失败时给用户的回复只包含：业务目标、当前状态、下一步建议；不包含任何实现细节。

### 8. 故障速查

| 现象 | 最可能原因 | 解决 |
| --- | --- | --- |
| `k3_search_forms` 返回 `NO_CANDIDATES` | 该领域不在静态目录内 | 用 `k3_describe_form` 验证 3. 实测字段知识中的标准 FormId |
| `k3_describe_form` 报 metadata unavailable | FormId 在当前账套不存在 | 换候选或向用户澄清 |
| `列名 'FID' 无效` | 该表无 FID 主键 | 在 order 参数中显式指定排序字段 |
| 字段不存在 | 账套未启用该字段 | `k3_describe_form` 换真实字段 Key |
| 查询成功但结果为空 | 条件过严或确无数据 | 放宽过滤 + limit=5 验证通道 |
| `K3 query row contains a business error` | 消息只透出错误 ID 时是旧包；新包带金蝶 Errors | 看新错误字段；涉及过滤条件时改成显式字段比较，不要用“不加过滤”表述 |
| k3_query_data / k3_query_report 返回权限拒绝 | 员工权限规则不匹配 | 联系管理员检查权限规则或切换测试身份 |
| 查询返回澄清请求 | 账簿/期间/状态等关键参数不明确 | 回答澄清问题，不要跳过 |
| `k3_search_forms` 搜不到刚才 describe 过的表 | 知识缓存尚未生效或已被清理 | 直接用已知 FormId 调 `k3_query_data`，不要重复搜索 |

### 9. 连续失败停止规则

- 对同一用户业务目标，金蝶固定工具、`k3_query_data` 或 `k3_query_report` 连续失败 **3 次**，必须停止继续换过滤条件、换 FormId、换字段或重复试探。
- 立即向用户报告：已尝试的最接近 FormId / 字段组合、最后一次完整错误信息、当前阻塞点、需要后端或用户确认的具体问题。
- 不要把语义不等的查询结果当作原始请求完成；可以先说明缺口并请求确认，禁止为了给出结果而降级。
