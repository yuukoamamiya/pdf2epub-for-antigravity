# 2026-10-28 英中词表术语调整应用记录

依据本轮授权，应用 2026-10-27 审计中两项 `change_suggested` 建议。此前词表已按独立删除记录移除5项证据不足条目，共153项；本次新增一项理论词条，最终共154项。

## 调整

- `long-waves`：目标由“长波理论”改为“长波”；移除 variants `long wave theories`、`long wave theory`。该条现在只指长波现象。
- 新增 `long-wave-theory`：`long wave theory` → “长波理论”，variant 为 `long wave theories`，类别为 `cyclical theory`；说明其指解释长波现象的理论。
- `nexus-rerum`：目标由“事物联系”改为“物的神经”。保留原有来源说明，并注明该译法用于《资本论》引文，拉丁语字面含义更广泛地指事物之间的联系，不将其规定为所有语境的固定译法。

## 文件与校验

- 正式词表：`glossaries/marxist-political-economy-en-zh.yaml`
- 应用前 SHA-256（153项，含此前5项删除）：`4b0b0909a4c8bcf231a81efd437a0e0764e1ad1019eb214a806385d7acbe6272`
- 应用后 SHA-256：`5012fb101a68469de9bc37b8835cdf78f65253211e0098a361e5a4cbedad5632`
- 应用后词条数：154
- 保持版本 `2026-10-28`；YAML 保持 CRLF 行尾。
- 2026-10-27 JSON 的158条历史审计和原 SHA 已保留，另追加本次应用元数据；5项删除仍由 `marxist-political-economy-en-zh-removals-2026-10-28.md` 单独记录。


## Follow-up application: contextual terms and variants

本次 follow-up 在前一节记录的两项改译、`long waves` / `long wave theory` 拆分及5项删除之后继续应用；历史审计的158条记录和原始SHA不变。正式词表仍为154项，版本仍为 `2026-10-28`。

### Target 调整

| ID | Source | 修改前 | 修改后 |
|---|---|---|---|
| `bank-rate` | Bank Rate | 银行官定利率 | 银行利率 |
| `neoclassical-synthesis` | neoclassical synthesis | 新古典综合 | 新古典综合派 |
| `real-abstraction` | real abstraction | 真实抽象 | 现实抽象 |
| `real-bills-doctrine` | real bills doctrine | 真实票据原则 | 真实票据理论 |

`real-abstraction` 注释保留“真实抽象”为异译；`real-bills-doctrine` 注释保留“真实票据原则”为有可靠用例的异译。`neoclassical-synthesis` 按 `economic school` 类别采用“新古典综合派”，并说明泛指 synthesis 可译“新古典综合”。

`bank-rate` 的旧注释为：“Political Economy of Money and Finance: 中央银行向商业银行提供贴现或再贷款所收取的基准贴现率”本次改为区分历史英国贴现/再贴现制度与现代英国 Bank Rate（英格兰银行向存放资金的商业银行付息的政策利率），没有把现代 Bank Rate 一概解释为央行贷款贴现率。来源核查由独立子代理完成；本记录不声称本执行者另行查询：

- Cambridge Dictionary: https://dictionary.cambridge.org/us/dictionary/english-chinese-simplified/bank-rate
- Bank of England: https://www.bankofengland.co.uk/monetary-policy/the-interest-rate-bank-rate

### Variant 调整

以下23项不等义或带有额外范围限制的 variants 已从正式匹配别名中移除；每个词条原有定义均保留，并追加语境说明。

| ID | Source | 移除 variants | 保留/新增 |
|---|---|---|---|
| `abstract-labor` | abstract labor | abstract human labor; abstract human labour; equal human labor | 保留 abstract labour |
| `classical-political-economy` | classical political economy | classical economy | 注释保留其历史简称提示 |
| `commodity-fetishism` | commodity fetishism | commodity fetish | — |
| `complex-labor` | complex labor | skilled labor | — |
| `concrete-labor` | concrete labor | concrete useful labor; concrete useful labour; concrete, useful labor; useful labor | 新增英式拼写 concrete labour |
| `downswing` | downswing | long downswing | — |
| `fetish-character` | fetish character | fetish character of the commodity | — |
| `money-supply-endogeneity` | money supply endogeneity | endogenous money; endogenous money supply | — |
| `money-supply-exogeneity` | money supply exogeneity | exogenous money; exogenous money supply | — |
| `simple-labor` | simple labor | simple human labor; simple unskilled labor; unskilled labor | 保留 simple labour |
| `valorization` | valorization | process of valorization; valorization process; Verwertungsprozess | 保留 Verwertung |
| `world-money` | world money | universal money | — |

对全部23个 contextual 条目逐项核对：本次为21项追加或完善语境说明；`free banking` 与 `reflux` 原注释已明确区分对应概念，保留原文不重复追加。除 Bank Rate 按上文校准定义外，其余说明均保留原注释并追加中文限定。未改变总词条数，也未添加词条。

### 应用后状态

- 目标调整：4项；移除 variants：23项；新增纯拼写 variant：`concrete labour`；新建词条：0。
- 当前词条数：154；版本：`2026-10-28`；YAML 行尾：CRLF。
- 本次修改前 SHA-256：`5012fb101a68469de9bc37b8835cdf78f65253211e0098a361e5a4cbedad5632`。
- 本次修改后 SHA-256：`e5f3425727d9310c3cf10d127c209f7d0f6617f618417f18d910330c38dcd160`。
