# 德文马克思主义术语表联网核对（2026-10-28）

核对并落实德文术语表126条历史基线的审核结论。六个证据不足条目已删除，六项译名建议已采用，19组明确不等义变体已清理；完整变更映射见文末“落实记录”及配套JSON。英文表的5项删除已单独提交为 `9b7d9b5`。

历史基线：`glossaries/marxist-theory-de-zh.yaml`，版本 `2026-10-21`，共126条。当前词表版本为 `2026-10-28`，落实后共120条。

SHA-256：`cc3d62353df8623fc32b34eca5cfa52f819c9b73d420b0919ee8dcb409382a48`。

## 核对结果

| 状态 | 条数 |
| --- | ---: |
| 已确认 | 63 |
| 限语境或变体需处理 | 50 |
| 建议修改 | 7 |
| 证据不足 | 6 |

## 优先处理建议

| 德文 | 当前译法 | 建议 |
| --- | --- | --- |
| Charaktermaske | 人格面具 | 性格面具；商务印书馆HKWM中译目录采用（性格）面具，建议本表优先用性格面具。人格面具也见于部分马克思主义中文讨论，不能由辞典差异推断为绝对错误；应避免与荣格Persona概念混淆。 |
| Erscheinungsform | 显现形式 | 表现形式；权威辞典与高校论文支持“表现形式”，建议替换“显现形式”。 |
| formelle Subsumtion | 形式隶属 | 形式从属；形式隶属与形式从属都有用例，检索到大学研究采用后者。 |
| Kapital im Allgemeinen | 一般资本 | 资本一般；INKRIT多语词条直接列中文“资本一般”；CSSN同批结果也使用“资本一般”。当前词表“一般资本”可考虑修正。 |
| reelle Subsumtion | 实际隶属 | 实际从属；建议首选实际从属，以与形式从属成对；当前实际隶属可理解，但本轮核到的中文研究直接采用实际从属。此项为术语一致性建议。 |
| Transformationsproblem | 转型问题 | 转形问题；Chinese political-economy scholarship uses 转形问题 for the value-to-production-price problem; 转型问题 is not preferred here. |
| Verdinglichung | 物化 | 物化；物化对应Verdinglichung可保留；修改建议针对变体：Versachlichung应按研究语境另列物象化、事物化等译法，不宜作为无条件同义变体。 |

证据不足项：dialektische Darstellung（辩证叙述）、Geld als Selbstzweck（货币作为目的本身）、Produktionskostentheorie（生产费用论）、Scheinausgleich（表面调和）、subjektive Arbeitswertlehre（主观劳动价值论）、theoretisches Feld（理论场域）。

变体审查与主译是否通行分开判断：如Bourgeois/Proletarier是个体词，Eigentum不必然为私有，Versachlichung与Verdinglichung不宜混并，形容词和有限定的复合词应保留句法与限定。

## 方法与限制

- confirmed表示找到符合当前语境的中译证据，不表示唯一译法或国家审定术语，也不等于词频统计。
- contextual表示主译可用但有语境、搭配、来源强度或变体范围限制；不能与证据不足混为一谈。
- change_suggested记录当时提出的译名或变体建议；已授权落实的项目已写入当前德文表，具体差异以文末落实记录为准。
- unverified表示本轮网络材料不足以确认中文通行性，不代表词义错误或德文概念不存在。
- 经典原文与中译相同章节/段落的对应可构成证据，无需在同一个网页出现德中双语。
- 部分条目只获取搜索摘要；上传书摘和普通词典仅按有限来源处理。本地书稿未作为网络通行性证据。

## 逐条记录

### 1. absoluter Mehrwert → 绝对剩余价值

ID：`absoluter-mehrwert`；状态：**已确认**；建议译法：绝对剩余价值。

德文术语与中文“绝对剩余价值”直接对应；其定义为在必要劳动之外延长剩余劳动。

实际查询：

- `absoluter Mehrwert 绝对剩余价值 德文 中文 Marx`
- `"绝对剩余价值" "absoluter Mehrwert" 资本论 中文`
- `"absolute Mehrwert" "绝对剩余价值" 德文 资本论`

来源及支持范围：

- [Absoluter Mehrwert](https://www.exit-online.org/absoluter-mehrwert/)：德文条目解释绝对剩余价值通过延长工作日等方式增加；未呈现中文对应。（理论网站；检索结果摘要；未打开全文核验）
- [剩余价值词条](https://knowecon.com/wiki/%E5%89%A9%E4%BD%99%E4%BB%B7%E5%80%BC)：词条直接将 absoluter Mehrwert 对应“绝对剩余价值”，并解释其通过延长工作日等增加剩余劳动。（术语百科；搜索结果摘要；链接可供全文复核）

### 2. abstrakt menschliche Arbeit → 抽象人类劳动

ID：`abstrakt-menschliche-arbeit`；状态：**已确认**；建议译法：抽象人类劳动。

检索到的中译材料与当前词义大体一致。

现有变体：abstrakte Arbeit。

变体意见：需按语境核实该变体是否与主词等义。

实际查询：

- `abstrakt menschliche Arbeit 抽象人类劳动 德文 中文 Marx`

来源及支持范围：

- [第一章 商品](https://www.marxists.org/chinese/marx/capital/01.htm)：《资本论》译文出现“抽象人类劳动”。（经典文本中译；检索结果摘要；未打开全文核验）
- [现代哲学2014年第4期](https://mphilosophy.sysu.edu.cn/sites/default/files/2020-06/%E7%8E%B0%E4%BB%A3%E5%93%B2%E5%AD%A62014%E7%AC%AC4%E6%9C%9F.pdf)：论文并列 abstrakt menschliche Arbeit 与抽象人类劳动。（高校论文；检索结果摘要；未打开全文核验）

### 3. Akkumulation des Kapitals → 资本积累

ID：`akkumulation-des-kapitals`；状态：**已确认**；建议译法：资本积累。

此处是一般范畴 Akkumulation des Kapitals，译“资本积累”。《资本积累论》仅是卢森堡同名专著的中文书名，不应据此给一般术语加“论”。

实际查询：

- `Akkumulation des Kapitals 资本积累 德文 中文 Marx`
- `"Akkumulation des Kapitals" "资本积累" 马克思 术语`
- `"Die Akkumulation des Kapitals" "资本积累" "资本论" 马恩全集`
- `"剩余价值再转化为资本" "资本积累" 马克思`

来源及支持范围：

- [资本积累论（中文）](https://www.marxists.org/chinese/rosa-luxemburg/1913/08.htm)：中文书名《资本积累论》对应德文 Die Akkumulation des Kapitals。（经典文本中译；检索结果摘要；未打开全文核验）
- [德文原典](https://www.marxists.org/deutsch/archiv/luxemburg/1913/akkkap/index.htm)：德文原书名。（经典文本原文；检索结果摘要；未打开全文核验）
- [罗莎·卢森堡《资本积累论》译后记](https://www.marxists.org/chinese/rosa-luxemburg/1913/33.htm)：说明 Die Akkumulation des Kapitals 是卢森堡著作原名，中文书名《资本积累论》；这只是专名证据，不决定本条一般概念译名。（经典著作译后记；搜索结果摘要；链接可供全文复核）
- [《资本论》第二十二章：剩余价值转化为资本](https://www.marxists.org/chinese/marx/capital/22.htm)：中文经典文本明确把“把剩余价值再转化为资本”称为“资本积累”，支持一般概念 target；不是卢森堡书名。（经典文本中译；搜索结果摘要；建议点击链接复核全文）

### 4. allgemeines Äquivalent → 一般等价物

ID：`allgemeines-aquivalent`；状态：**已确认**；建议译法：一般等价物。

《资本论》价值形式分析中的一般等价物，中文术语为“一般等价物”；已找到术语资料和经典文本对应章节。

实际查询：

- `allgemeines Äquivalent 一般等价物 德文 中文 Marx`
- `"allgemeines Äquivalent" "一般等价物" 马克思 资本论 中文`

来源及支持范围：

- [Zur Kritik der Politischen Ökonomie](https://biancahoegel.de/archiv/marx_engels/kritik_pol_oek/erstes_kapitel.html)：原文有 allgemeines Äquivalent，但本次未找到可靠中文同段版本。（德文经典文本；检索结果摘要；未打开全文核验）
- [一般等价物词条](https://knowecon.com/wiki/%E4%B8%80%E8%88%AC%E7%AD%89%E4%BB%B7%E7%89%A9)：给出德文 Allgemeines Äquivalent 与中文“一般等价物”对应；二手术语资料。（术语百科；检索结果摘要；未打开全文核验）
- [《资本论》第一卷第一章（中译）](https://www.marxists.org/chinese/marx/capital/01.htm)：中文原典讨论一般等价形式、商品与货币的关系；与该章德文原词一般等价物段落对应。（经典文本中译；搜索结果摘要；链接可供全文复核）

### 5. Arbeitskraft → 劳动力

ID：`arbeitskraft`；状态：**已确认**；建议译法：劳动力。

检索到的中译材料与当前词义大体一致。

实际查询：

- `Arbeitskraft 劳动力 德文 中文 Marx`

来源及支持范围：

- [资本论第四章](https://www.marxists.org/chinese/marx/capital/04.htm)：中文原典使用“劳动力”；并将其释为劳动力或劳动能力。（经典文本中译；检索结果摘要；未打开全文核验）

### 6. Basis → 基础

ID：`basis`；状态：**限语境或变体需处理**；建议译法：基础。

Basis可泛指基础；materielle/ökonomische Basis是有限定的短语，不宜无语境合并。

现有变体：materielle Basis；ökonomische Basis。

变体意见：materielle Basis、ökonomische Basis分别含物质、经济限定，不能统一丢弃限定语。

实际查询：

- `Basis 基础 德文 中文 Marx`

来源及支持范围：

- [北京大学：经济基础与上层建筑](https://marxism.pku.edu.cn/xzlt/9mkszyxy1340591.htm)：解释“经济基础”为经济结构/生产关系总和，说明基础需结合限定语境。（高校研究；检索结果摘要；未打开全文核验）

### 7. Bewußtsein → 意识

ID：`bewutsein`；状态：**已确认**；建议译法：意识。

检索到的中译材料与当前词义大体一致。

实际查询：

- `Bewußtsein 意识 德文 中文 Marx`

来源及支持范围：

- [马克思主义历史考证大辞典第二卷](https://www.cp.com.cn/book/30e31e6b-2.html)：词条目录直接对应“意识（Bewußtsein）”。（权威术语辞典；检索结果摘要；未打开全文核验）

### 8. Bourgeoisie → 资产阶级

ID：`bourgeoisie`；状态：**限语境或变体需处理**；建议译法：资产阶级。

Bourgeoisie指阶级集体，Bourgeois多指个体资产者；不应作为无差别变体。

现有变体：Bourgeois。

变体意见：Bourgeois指资产者个体，Bourgeoisie指资产阶级，建议移除个体词作为等价变体。

实际查询：

- `Bourgeoisie 资产阶级 德文 中文 Marx`

来源及支持范围：

- [资产阶级的翻译及其中国化](https://xbshkx.cdut.edu.cn/cn/article/pdf/preview/10.3969/j.issn.1672-0539.2020.01.020.pdf)：研究梳理 Bourgeoisie 的中文译名演变，资产阶级为通行译词。（学术期刊论文；检索结果摘要；未打开全文核验）
- [资产者的问题](https://ptext.nju.edu.cn/info/1181/3488.htm)：明确区分 bourgeois（资产者）与 bourgeoisie（资产阶级）。（高校研究；检索结果摘要；未打开全文核验）

### 9. bürgerliche Gesellschaft → 市民社会

ID：`burgerliche-gesellschaft`；状态：**限语境或变体需处理**；建议译法：市民社会。

可译市民社会，也可能依文本语境译资产阶级社会。

现有变体：Civil Society。

变体意见：可译市民社会，也可能依文本语境译资产阶级社会。

实际查询：

- `bürgerliche Gesellschaft 市民社会 德文 中文 Marx`

来源及支持范围：

- [马克思的市民社会概念](https://xsyk.cbpt.cnki.net/portal/journal/portal/client/paper/24ff8f0603fd209bad070b6261996f74)：文中直接并列 bürgerliche Gesellschaft 与市民社会，并说明特定语境可指现代资产阶级社会。（学术期刊论文；检索结果摘要；未打开全文核验）

### 10. Charaktermaske → 人格面具

ID：`charaktermaske`；状态：**建议修改**；建议译法：性格面具。

商务印书馆HKWM中译目录采用（性格）面具，建议本表优先用性格面具。人格面具也见于部分马克思主义中文讨论，不能由辞典差异推断为绝对错误；应避免与荣格Persona概念混淆。

实际查询：

- `Charaktermaske 人格面具 德文 中文 Marx`

来源及支持范围：

- [马克思主义历史考证大辞典第二卷](https://www.cp.com.cn/book/30e31e6b-2.html)：辞典目录译作“（性格）面具（Charaktermaske）”，没有支持“人格面具”的直接证据。（权威术语辞典；检索结果摘要；未打开全文核验）
- [列斐伏尔与社会批判理论](https://ptextold.nju.edu.cn/15/37/c13372a595255/page.htm)：马克思主义研究语境中也有使用人格面具的例子，但该用例不是直接德中词对。（学术研究；搜索结果摘要；未逐页全文核读）

### 11. Darstellungsmethode → 叙述方法

ID：`darstellungsmethode`；状态：**已确认**；建议译法：叙述方法。

检索到的中译材料与当前词义大体一致。

实际查询：

- `Darstellungsmethode 叙述方法 德文 中文 Marx`

来源及支持范围：

- [马克思《资本论》的研究方法和叙述方法再审视](https://cnki.istiz.org.cn/kcms/detail/detail.aspx?dbcode=CJFD&dbname=CJFD2025&filename=NMGR202503009)：中文论文使用“叙述方法”。（学术论文；检索结果摘要；未打开全文核验）

### 12. Der Einzige → 唯一者

ID：`der-einzige`；状态：**已确认**；建议译法：唯一者。

检索到的中译材料与当前词义大体一致。

现有变体：Einzige。

变体意见：需按语境核实该变体是否与主词等义。

实际查询：

- `Der Einzige 唯一者 德文 中文 Marx`

来源及支持范围：

- [德意志意识形态](https://www.marxists.org/chinese/pdf/marx-engels/mes01.pdf)：编者注提到施蒂纳著作《唯一者及其所有物》，支持唯一者的中文用法。（经典文本/编注；检索结果摘要；未打开全文核验）

### 13. dialektische Darstellung → 辩证叙述

ID：`dialektische-darstellung`；状态：**证据不足**；建议译法：辩证叙述。

检索发现低可信 AI 术语文稿列出“辩证叙述”，但未找到可采信的学术或经典文本对译；保持 unverified。

实际查询：

- `"dialektische Darstellung" 辩证叙述 马克思 中文 翻译`
- `"dialektische Darstellung" "辩证叙述" 《资本论》中文 术语`

来源及支持范围：

- [Ferrer AI Textual 术语清单](https://philarchive.org/archive/FERFAT-10)：页面列出 Dialektische Darstellung—辩证叙述；来源为AI辅助术语文稿，权威性低，仅作线索，不足以确认标准译法。（低可信二手资料；搜索结果摘要；需全文复核）

### 14. Doppelcharakter der Arbeit → 劳动的二重性

ID：`doppelcharakter-der-arbeit`；状态：**已确认**；建议译法：劳动的二重性。

检索到的中译材料与当前词义大体一致。

实际查询：

- `Doppelcharakter der Arbeit 劳动的二重性 德文 中文 Marx`

来源及支持范围：

- [马克思主义历史考证大辞典第二卷](https://www.cp.com.cn/book/30e31e6b-2.html)：辞典目录直接列“劳动的二重性（Doppelcharakter der Arbeit）”。（权威术语辞典；检索结果摘要；未打开全文核验）

### 15. Durchschnittsprofit → 平均利润

ID：`durchschnittsprofit`；状态：**已确认**；建议译法：平均利润。

检索到的中译材料与当前词义大体一致。

实际查询：

- `Durchschnittsprofit 平均利润 德文 中文 Marx`

来源及支持范围：

- [马恩全集第二十五卷第九章](https://www.marxists.org/chinese/marx-engels/25/010.htm)：中文原典直接使用平均利润。（经典文本中译；检索结果摘要；未打开全文核验）

### 16. Durchschnittsprofitrate → 平均利润率

ID：`durchschnittsprofitrate`；状态：**限语境或变体需处理**；建议译法：平均利润率。

原典标题并列一般利润率/平均利润率；按具体定义和语境处理。

现有变体：allgemeine Profitrate。

变体意见：原典标题并列一般利润率/平均利润率；按具体定义和语境处理。

实际查询：

- `Durchschnittsprofitrate 平均利润率 德文 中文 Marx`

来源及支持范围：

- [马恩全集第二十五卷第九章](https://www.marxists.org/chinese/marx-engels/25/010.htm)：章节名并列一般利润率（平均利润率），说明存在语境差别。（经典文本中译；检索结果摘要；未打开全文核验）

### 17. einfache Arbeit → 简单劳动

ID：`einfache-arbeit`；状态：**已确认**；建议译法：简单劳动。

《资本论》第一卷商品章将 einfache Arbeit 译为“简单劳动”，并与简单平均劳动范畴相连。

实际查询：

- `einfache Arbeit 简单劳动 德文 中文 Marx`
- `"einfache Arbeit" "简单劳动" 术语 翻译 Marx`
- `"einfache Arbeit" "简单劳动" 资本论`
- `"einfache Arbeit" "简单劳动" 资本论 第一章 中文`

来源及支持范围：

- [德文术语条目](https://inkrit.de/e_inkritpedia/e_maincode/doku.php?id=a%3Aabstrakte_arbeit)：德文资料引述 einfache Arbeit，但未给中文对应。（术语辞典；检索结果摘要；未打开全文核验）
- [《资本论》第一卷第一章（中译）](https://www.marxists.org/chinese/marx/capital/01.htm)：中文原典直接使用“简单劳动”，并描述其作为不同劳动的计量单位；与该章德文 einfache Arbeit 对应。（经典文本中译；搜索结果摘要；链接可供全文复核）
- [《资本论》第一卷第一章（德文）](https://marx-zh-cn.github.io/docs/MEW-ZENO/13/ME13-015.html)：德文原文出现“Es ist einfache Arbeit, wozu jedes Durchschnittsindividuum abgerichtet werden kann”。（经典文本原文；搜索结果摘要；链接可供全文复核）

### 18. einfache Zirkulation → 简单流通

ID：`einfache-zirkulation`；状态：**已确认**；建议译法：简单流通。

检索到的中译材料与当前词义大体一致。

实际查询：

- `einfache Zirkulation 简单流通 德文 中文 Marx`

来源及支持范围：

- [einfache Zirkulation 术语条目](https://inkrit.de/e_inkritpedia/e_maincode/doku.php?id=e%3Aeinfache_zirkulation)：术语条目给出中文“简单流通”。（术语辞典；检索结果摘要；未打开全文核验）

### 19. Entfremdung → 异化

ID：`entfremdung`；状态：**限语境或变体需处理**；建议译法：异化。

Entfremdung与Entäußerung相关但并非任何语境下都可互换；建议移除同义变体或明确各自译法。

现有变体：entfremdet；Entäußerung。

变体意见：entfremdet为异化的；Entäußerung有外化、异化等译法，应单独处理。

实际查询：

- `Entfremdung 异化 德文 中文 Marx`

来源及支持范围：

- [社会对抗：马克思交往异化思想的实质](https://xb.szu.edu.cn/article/2016/1000-260X-33-5-53.html)：直接指出 Entfremdung 与 Entäußerung 的译法及语义不可简单混同。（大学学报论文；检索结果摘要；未打开全文核验）
- [1844年经济学哲学手稿](https://www.marxists.org/chinese/marx/marxist.org-chinese-marx-1844.htm)：中译文并用异化、外化。（经典文本中译；检索结果摘要；未打开全文核验）

### 20. epistemologischer Bruch → 认识论断裂

ID：`epistemologischer-bruch`；状态：**已确认**；建议译法：认识论断裂。

检索到的中译材料与当前词义大体一致。

实际查询：

- `epistemologischer Bruch 认识论断裂 德文 中文 Marx`

来源及支持范围：

- [晚期阿尔都塞对认识论断裂的自我突破](https://zxyj.cbpt.cnki.net/portal/journal/portal/client/paper/84d3cbef7a3ee0cbd99e5ac6ab2140fd)：直接使用“认识论断裂”。（中文核心期刊；检索结果摘要；未打开全文核验）

### 21. Erscheinungsform → 显现形式

ID：`erscheinungsform`；状态：**建议修改**；建议译法：表现形式。

权威辞典与高校论文支持“表现形式”，建议替换“显现形式”。

实际查询：

- `Erscheinungsform 显现形式 德文 中文 Marx`

来源及支持范围：

- [马克思主义历史考证大辞典第三卷](https://www.cp.com.cn/book/fa542a86-d.html)：辞典对 Erscheinungsform 的对应译法是“表现形式”。（权威术语辞典；检索结果摘要；未打开全文核验）
- [现代哲学2014年第4期](https://mphilosophy.sysu.edu.cn/sites/default/files/2020-06/%E7%8E%B0%E4%BB%A3%E5%93%B2%E5%AD%A62014%E7%AC%AC4%E6%9C%9F.pdf)：论文明确对照 Erscheinungsform/表现形式。（高校论文；检索结果摘要；未打开全文核验）

### 22. Fetischcharakter → 拜物教性质

ID：`fetischcharakter`；状态：**限语境或变体需处理**；建议译法：拜物教性质。

证据对应完整搭配“商品的拜物教性质”；可在词条注释中标明搭配。

实际查询：

- `Fetischcharakter 拜物教性质 德文 中文 Marx`

来源及支持范围：

- [Fetischismus 及相关词在马克思著作中的话语变迁](https://mphilosophy.sysu.edu.cn/sites/default/files/2020-06/%E7%8E%B0%E4%BB%A3%E5%93%B2%E5%AD%A62017%E7%AC%AC1%E6%9C%9F.pdf)：论文将 Der Fetischcharakter der Ware 对应为“商品的拜物教性质”。（高校论文；检索结果摘要；未打开全文核验）

### 23. fiktives Kapital → 虚拟资本

ID：`fiktives-kapital`；状态：**已确认**；建议译法：虚拟资本。

检索到的中译材料与当前词义大体一致。

实际查询：

- `fiktives Kapital 虚拟资本 德文 中文 Marx`

来源及支持范围：

- [虚拟资本理论研究导论](https://marxism.ssap.com.cn/skwx_marxism/LiteratureDetail.aspx?ID=153775)：社科文献出版社研究数据库使用“虚拟资本”。（学术专著/数据库；检索结果摘要；未打开全文核验）

### 24. formelle Subsumtion → 形式隶属

ID：`formelle-subsumtion`；状态：**建议修改**；建议译法：形式从属。

形式隶属与形式从属都有用例，检索到大学研究采用后者。

实际查询：

- `formelle Subsumtion 形式隶属 德文 中文 Marx`

来源及支持范围：

- [形式隶属专题研究](https://ptextold.nju.edu.cn/57/05/c13164a612101/page.htm)：南京大学研究使用“形式从属”，构成当前“形式隶属”的异译证据。（高校研究；检索结果摘要；未打开全文核验）

### 25. Forschungsmethode → 研究方法

ID：`forschungsmethode`；状态：**已确认**；建议译法：研究方法。

检索到的中译材料与当前词义大体一致。

实际查询：

- `Forschungsmethode 研究方法 德文 中文 Marx`

来源及支持范围：

- [马克思的研究方法新论](https://www.dswxyjy.org.cn/n1/2019/0617/c427184-31158188.html)：中文论文固定使用“研究方法”。（理论研究文章；检索结果摘要；未打开全文核验）

### 26. Gattungswesen → 类本质

ID：`gattungswesen`；状态：**限语境或变体需处理**；建议译法：类本质。

Gattungswesen译作类本质/类存在物均见；Gattung单独是“类”，建议删除该变体。

现有变体：Gattung。

变体意见：Gattung通常是类，并不等于类本质，应移出等价变体。

实际查询：

- `Gattungswesen 类本质 德文 中文 Marx`

来源及支持范围：

- [1844年经济学哲学手稿](https://www.marxists.org/chinese/marx-engels/42/003.htm)：经典中译使用“类存在物”；该术语也有“类本质”译法。（经典文本中译；检索结果摘要；未打开全文核验）
- [昆明学院学报2022](https://xb.kmu.edu.cn/22412.pdf)：论文指出“类本质”可能遮蔽 Wesen 的存在义。（学术期刊论文；检索结果摘要；未打开全文核验）

### 27. Gebrauchswert → 使用价值

ID：`gebrauchswert`；状态：**已确认**；建议译法：使用价值。

检索到的中译材料与当前词义大体一致。

实际查询：

- `Gebrauchswert 使用价值 德文 中文 Marx`

来源及支持范围：

- [资本论第一卷节选](https://www.htu.edu.cn/xsd/2021/0414/c14329a193828/page.htm)：中文文本使用“使用价值”。（高校经典文本选编；检索结果摘要；未打开全文核验）

### 28. Geld als Geld → 货币作为货币

ID：`geld-als-geld`；状态：**已确认**；建议译法：货币作为货币。

《资本论》第一卷提纲的中文原典直接使用“货币作为货币”，并与“货币作为资本”对照；当前译法可确认。

实际查询：

- `"Geld als Geld" "货币作为货币" 马克思`
- `"Geld als Geld" 货币作为货币 马克思 中文 术语`
- `"货币作为货币" "货币作为资本" 马克思 中文原典`

来源及支持范围：

- [卡·马克思《资本论》第一卷提纲](https://www.marxists.org/chinese/marx-engels/16/039.htm)：中文原典直接区分“货币作为货币的流通”与“货币作为资本的流通”。（经典文本中译；搜索结果摘要；建议点击链接复核全文）

### 29. Geld als Selbstzweck → 货币作为目的本身

ID：`geld-als-selbstzweck`；状态：**证据不足**；建议译法：货币作为目的本身。

此条指货币作为独立财富/目的本身的特殊规定；目前检索到的材料仅能确认德文表述，未找到足以确认当前中文措辞的可靠中译，故维持 unverified。

实际查询：

- `"Geld als Selbstzweck" 马克思 货币 目的本身`
- `"Geld als Selbstzweck" "货币作为目的本身"`
- `"Geld als Selbstzweck" Heinrich Marx Geld`

来源及支持范围：

- [Karl Marx: Urtext（1858），Geld als Selbstzweck](https://external.dandelon.com/download/attachments/dandelon/ids/AT001A74E9CC7C6F86007C1257E1800441172.pdf)：德文篇名直接出现 Geld als Selbstzweck；仅证明德文术语用法，未证明当前中文译法。（德文研究目录；搜索结果摘要；建议点击链接复核全文）

### 30. Geldfetisch → 货币拜物教

ID：`geldfetisch`；状态：**限语境或变体需处理**；建议译法：货币拜物教。

论文直接对应货币拜物教与Geldfetisch，同时提出货币物神的另一译法；当前译名有依据，需留意Fetisch与Fetischismus的区别。

实际查询：

- `Geldfetisch 货币拜物教 德文 中文 Marx`
- `"Geldfetisch" "货币拜物教"`

来源及支持范围：

- [Fetischismus及相关词在马克思著作中的话语变迁](https://mphilosophy.sysu.edu.cn/sites/default/files/2020-06/%E7%8E%B0%E4%BB%A3%E5%93%B2%E5%AD%A62017%E7%AC%AC1%E6%9C%9F.pdf)：论文直接将Geldfetisch对应货币拜物教，并讨论货币物神译法。（学术研究；搜索结果摘要；未逐页全文核读）

### 31. Geldform → 货币形式

ID：`geldform`；状态：**已确认**；建议译法：货币形式。

南京大学研究文章明确写“货币形式（Geldform）”，德汉词条也给出“货币形式”；当前中译有直接支持。

实际查询：

- `Geldform 货币形式 德文 中文 Marx`
- `"Geldform" "货币形式" 术语 翻译 Marx`
- `"Geldform" "货币形式" 马克思 中文`

来源及支持范围：

- [价值形式的商品—货币拜物教之谜](https://ptextold.nju.edu.cn/a0/91/c13164a630929/page.htm)：研究文章写出货币形式（Geldform）。（高校研究；检索结果摘要；未打开全文核验）
- [德语助手 Geldform 词条](https://www.godic.net/dicts/de/Geldform)：双语词典将 Geldform 释为货币形式。（双语词典；检索结果摘要；未打开全文核验）

### 32. Geldkrise → 货币危机

ID：`geldkrise`；状态：**已确认**；建议译法：货币危机。

MEGA II-10 术语对照表列 Geldkrise 并给出“货币危机”对应（另有金融恐慌、货币恐慌等历史译法）；当前词条译法有直接依据，历史译词差异可记为上下文信息。

实际查询：

- `"Geldkrise" "货币危机" Marx 资本论`
- `"Geldkrise" "货币危机" 马克思 翻译`
- `"Geldkrise" "货币危机" MEGA 术语对照`

来源及支持范围：

- [MEGA术语对照表](https://tohoku.repo.nii.ac.jp/record/123869/files/160926-SHENG-167-1.pdf)：检索结果中的多语术语表列 Geldkrise 对应中文“货币危机”；属对照资料而非马克思原典。（学术对照资料；搜索结果摘要；需全文复核）

### 33. Geldware → 货币商品

ID：`geldware`；状态：**已确认**；建议译法：货币商品。

《资本论》中文正文直接出现“货币商品（Geldware）”；当前译法获经典中译直接支持。

实际查询：

- `Geldware 货币商品 德文 中文 Marx`
- `"Geldware" "货币商品" 术语 翻译 Marx`

来源及支持范围：

- [资本论第一卷第一篇商品与货币](https://www.diancangwang.cn/waiguomingzhu/ac82405c9cbe/909b5d49b4e0.html)：中文原典正文直接使用货币商品（Geldware）。（经典文本中译；检索结果摘要；未打开全文核验）

### 34. Geltung → 有效性

ID：`geltung`；状态：**限语境或变体需处理**；建议译法：有效性。

Geltung在价值理论、社会效力及法律语境译法不同，需标注上下文。

实际查询：

- `Geltung 有效性 德文 中文 Marx`

来源及支持范围：

- [马克思主义与批判理论：Geltung 译注](https://ptextold.nju.edu.cn/96/e2/c13379a562914/page.htm)：译注说明 Geltung 可译效力、有效等，需依文本语境。（高校研究/译注；检索结果摘要；未打开全文核验）

### 35. Gemeinwesen → 共同体

ID：`gemeinwesen`；状态：**限语境或变体需处理**；建议译法：共同体。

全集中 Gemeinwesen 有时译“共同体”、有时译“社会联系”；在《德意志意识形态》语境也有“共同体”用例。与 Gemeinschaft 的关系需按具体文本区分。

现有变体：Gemeinschaft。

变体意见：Gemeinschaft与Gemeinwesen有重合译法但概念不可直接互换。

实际查询：

- `Gemeinwesen 共同体 德文 中文 Marx`
- `"Gemeinwesen" "共同体" 术语 翻译 Marx`

来源及支持范围：

- [《德意志意识形态》共同体概念研究](https://mphilosophy.sysu.edu.cn/sites/default/files/2020-06/2009%E5%B9%B4%E7%AC%AC4%E6%9C%9F-%E6%80%BB.pdf)：指出 Gemeinwesen 有时译共同体、有时译社会联系，并与 Gemeinschaft 区分。（高校学术期刊；检索结果摘要；未打开全文核验）

### 36. gesellschaftlich notwendige Arbeitszeit → 社会必要劳动时间

ID：`gesellschaftlich-notwendige-arbeitszeit`；状态：**已确认**；建议译法：社会必要劳动时间。

检索到的中译材料与当前词义大体一致。

实际查询：

- `gesellschaftlich notwendige Arbeitszeit 社会必要劳动时间 德文 中文 Marx`

来源及支持范围：

- [资本论第一卷节选](https://www.htu.edu.cn/xsd/2021/0414/c14329a193828/page.htm)：原文定义社会必要劳动时间。（高校经典文本选编；检索结果摘要；未打开全文核验）

### 37. gesellschaftliches Sein → 社会存在

ID：`gesellschaftliches-sein`；状态：**限语境或变体需处理**；建议译法：社会存在。

社会存在是通用译名，但需区分 Sein 与 Dasein；变体 Sein 过宽。

现有变体：Sein。

变体意见：Sein是存在，缺少gesellschaftlich限定，不能自动译为社会存在。

实际查询：

- `gesellschaftliches Sein 社会存在 德文 中文 Marx`

来源及支持范围：

- [定在概念：马克思早期思想构境的历史线索](https://www.cssn.cn/mkszy/mkszy_mkszyfzs/202207/t20220728_5432941.shtml)：指出马克思较少使用 gesellschaftliches Sein，而常用 gesellschaftliches Dasein。（中国社会科学网论文；检索结果摘要；未打开全文核验）
- [马克思的社会存在概念](https://www.cssn.cn/zx/mkszyzx/202210/t20221027_5555216.shtml)：中文研究使用社会存在，同时讨论原典概念问题。（中国社会科学网论文；检索结果摘要；未打开全文核验）

### 38. Gesetz vom tendenziellen Fall der Profitrate → 利润率趋向下降的规律

ID：`gesetz-vom-tendenziellen-fall-der-profitrate`；状态：**限语境或变体需处理**；建议译法：利润率趋向下降的规律。

马克思原典章节标题使用“利润率趋向下降的规律”；变体省略 Gesetz，指该规律的趋势表述。

现有变体：tendenzieller Fall der Profitrate。

变体意见：tendenzieller Fall der Profitrate省略Gesetz，宜译利润率趋向下降，不自动补规律。

实际查询：

- `Gesetz vom tendenziellen Fall der Profitrate 利润率趋向下降的规律 德文 中文 Marx`

来源及支持范围：

- [马恩全集第二十五卷第三篇](https://www.marxists.org/chinese/marx-engels/25/014.htm)：章节标题直接使用“利润率趋向下降的规律”。（经典文本中译；检索结果摘要；未打开全文核验）

### 39. große Industrie → 大工业

ID：`groe-industrie`；状态：**已确认**；建议译法：大工业。

检索到的中译材料与当前词义大体一致。

现有变体：Großindustrie。

变体意见：需按语境核实该变体是否与主词等义。

实际查询：

- `große Industrie 大工业 德文 中文 Marx`

来源及支持范围：

- [德意志意识形态](https://www.marxists.org/chinese/pdf/marx-engels/mes01.pdf)：中文原典使用“大工业”。（经典文本中译；检索结果摘要；未打开全文核验）

### 40. herrschende Klasse → 统治阶级

ID：`herrschende-klasse`；状态：**已确认**；建议译法：统治阶级。

检索到的中译材料与当前词义大体一致。

实际查询：

- `herrschende Klasse 统治阶级 德文 中文 Marx`

来源及支持范围：

- [德意志意识形态](https://www.marxists.org/chinese/marx-engels2/01/16.htm)：中文原典使用“统治阶级”。（经典文本中译；检索结果摘要；未打开全文核验）

### 41. historischer Materialismus → 历史唯物主义

ID：`historischer-materialismus`；状态：**已确认**；建议译法：历史唯物主义。

“历史唯物主义”是该学说的中文通行术语。该名称的术语史涉及恩格斯的使用与定型，但不妨碍确认当前译名；Materialismus 单独不能作为本条变体。

实际查询：

- `historischer Materialismus 历史唯物主义 德文 中文 Marx`

来源及支持范围：

- [历史唯物主义的历史概念](https://marx.ruc.edu.cn/sy/mydj/5f137a579372480cad7f7607ea8b0605.htm)：研究指出历史唯物主义概念的命名由恩格斯发展定型，中文术语通行。（高校学者论文；检索结果摘要；未打开全文核验）

### 42. Idealismus → 唯心主义

ID：`idealismus`；状态：**限语境或变体需处理**；建议译法：唯心主义。

唯心主义是全集第二版译法；德哲史语境可有观念论/理念论等译法。

实际查询：

- `Idealismus 唯心主义 德文 中文 Marx`

来源及支持范围：

- [重思马克思对德国观念论的批判](https://www.cssn.cn/zx/zx_rdkx/202211/t20221103_5558777.shtml)：指出 Idealismus 可译唯心主义、理想主义、理念论、观念论；全集第二版使用唯心主义。（中国社会科学网论文；检索结果摘要；未打开全文核验）

### 43. Ideologie → 意识形态

ID：`ideologie`；状态：**限语境或变体需处理**；建议译法：意识形态。

文章讨论Ideologie曾译“观念体系”，并说明“意识形态”现已成为高频通行术语。

现有变体：ideologisch。

变体意见：ideologisch为形容词，需译作意识形态的等，不能机械套名词译法。

实际查询：

- `Ideologie 中文 意识形态 德意志意识形态 德文 汉译 学术`

来源及支持范围：

- [《德意志意识形态》“费尔巴哈”章的文本性质、文字判读和德文汉译](https://www.dswxyjy.org.cn/n1/2023/1030/c427184-40106071.html)：文章讨论Ideologie曾译“观念体系”，并说明“意识形态”现已成为高频通行术语。（high；真实搜索结果摘要；未逐页核读全文）

### 44. industrielle Reservearmee → 产业后备军

ID：`industrielle-reservearmee`；状态：**限语境或变体需处理**；建议译法：产业后备军。

中文原典用“产业后备军”，并在积累规律中讨论相对过剩人口；两者相关但应区分概念。

现有变体：relative Überbevölkerung。

变体意见：relative Überbevölkerung为相对过剩人口；与产业后备军关系密切，但不宜不加区分地替换。

实际查询：

- `industrielle Reservearmee 中文 产业后备军 马克思 原典`

来源及支持范围：

- [马恩全集第四十四卷——第二十三章 资本主义积累的一般规律](https://www.marxists.org/chinese/marx-engels-2/44/029.htm)：中文原典用“产业后备军”，并在积累规律中讨论相对过剩人口；两者相关但应区分概念。（high；真实搜索结果摘要；未逐页核读全文）

### 45. Kapital im Allgemeinen → 一般资本

ID：`kapital-im-allgemeinen`；状态：**建议修改**；建议译法：资本一般。

INKRIT多语词条直接列中文“资本一般”；CSSN同批结果也使用“资本一般”。当前词表“一般资本”可考虑修正。

实际查询：

- `Kapital im Allgemeinen 中文 资本一般 马克思`

来源及支持范围：

- [Kapital im Allgemeinen (INKRIT)](https://www.inkrit.de/neuinkrit/e_inkritpedia/e_maincode/doku.php?id=k%3Akapital_im_allgemeinen)：INKRIT多语词条直接列中文“资本一般”；CSSN同批结果也使用“资本一般”。当前词表“一般资本”可考虑修正。（high；真实搜索结果摘要；未逐页核读全文）

### 46. Kapitalfetisch → 资本拜物教

ID：`kapitalfetisch`；状态：**限语境或变体需处理**；建议译法：资本拜物教。

论文讨论“资本物神的观念”（Kapitalfetisch）也译作“资本拜物教的观念”；支持概念，译法形式有差异。

实际查询：

- `Kapitalfetisch 资本拜物教 Kapitalfetisch 中文 翻译`

来源及支持范围：

- [Fetischismus及相关词在马克思著作中的话语变迁](https://mphilosophy.sysu.edu.cn/sites/default/files/2020-06/%E7%8E%B0%E4%BB%A3%E5%93%B2%E5%AD%A62017%E7%AC%AC1%E6%9C%9F.pdf)：论文讨论“资本物神的观念”（Kapitalfetisch）也译作“资本拜物教的观念”；支持概念，译法形式有差异。（high；真实搜索结果摘要；未逐页核读全文）

### 47. Klasse → 阶级

ID：`klasse`；状态：**限语境或变体需处理**；建议译法：阶级。

原典译本在《德意志意识形态》语境使用“阶级”；此次检索摘要未提供词形逐句对照。

现有变体：Klassen。

变体意见：['须按词性和具体语境分别判断，不自动套用主词译法。']

实际查询：

- `Klasse Marx Klassen 阶级 德意志意识形态 中文`

来源及支持范围：

- [马克思恩格斯文集第一卷《德意志意识形态》](https://www.marxists.org/chinese/pdf/marx-engels/mes01.pdf)：原典译本在《德意志意识形态》语境使用“阶级”；此次检索摘要未提供词形逐句对照。（medium；真实搜索结果摘要；未逐页核读全文）

### 48. Kommunismus → 共产主义

ID：`kommunismus`；状态：**限语境或变体需处理**；建议译法：共产主义。

文章标题及摘要直接以“共产主义”讨论该书文本。

现有变体：kommunistisch。

变体意见：kommunistisch为形容词，需按句法处理为共产主义的等。

实际查询：

- `Kommunismus 共产主义 德意志意识形态 马克思 中文`

来源及支持范围：

- [《德意志意识形态》中的共产主义审视](https://xuebaosk.ahut.edu.cn/article/doi/10.3969/j.issn.1671-9247.2025.05.001?viewType=HTML)：文章标题及摘要直接以“共产主义”讨论该书文本。（high；真实搜索结果摘要；未逐页核读全文）

### 49. komplizierte Arbeit → 复杂劳动

ID：`komplizierte-arbeit`；状态：**已确认**；建议译法：复杂劳动。

文章明确讨论马克思复杂劳动，直接用“复杂劳动”作为术语。

实际查询：

- `komplizierte Arbeit 复杂劳动 资本论 中文`

来源及支持范围：

- [人大研究中心：马克思关于复杂劳动的论述](https://ztzx.ruc.edu.cn/xzgd/2de2f9204f7c4e0abba7f4161bb8247f.htm)：文章明确讨论马克思复杂劳动，直接用“复杂劳动”作为术语。（high；真实搜索结果摘要；未逐页核读全文）

### 50. konkrete Arbeit → 具体劳动

ID：`konkrete-arbeit`；状态：**已确认**；建议译法：具体劳动。

文章引用《资本论》价值形式分析，明确使用“具体劳动”。

实际查询：

- `konkrete Arbeit 具体劳动 资本论 中文`

来源及支持范围：

- [准确解读《资本论》的原理和方法](https://www.dswxyjy.org.cn/n1/2019/0617/c427160-31161996.html)：文章引用《资本论》价值形式分析，明确使用“具体劳动”。（high；真实搜索结果摘要；未逐页核读全文）

### 51. Konkurrenz der vielen Kapitalien → 许多资本的竞争

ID：`konkurrenz-der-vielen-kapitalien`；状态：**限语境或变体需处理**；建议译法：许多资本的竞争。

文章将Concurrenz der Capitalien说明为“诸资本的竞争”，指出德文复数表达多个资本。

实际查询：

- `Konkurrenz der vielen Kapitalien 中文 许多资本的竞争`

来源及支持范围：

- [中国人民大学研究中心：诸资本的积累、竞争与积聚](https://ztzx.ruc.edu.cn/xzgd/c53d60e81f9c4798976c2e920c8a835d.htm)：文章将Concurrenz der Capitalien说明为“诸资本的竞争”，指出德文复数表达多个资本。（high；真实搜索结果摘要；未逐页核读全文）

### 52. konstantes Kapital → 不变资本

ID：`konstantes-kapital`；状态：**已确认**；建议译法：不变资本。

原典中文标题及正文直接使用“不变资本”。

实际查询：

- `konstantes Kapital 不变资本 中文 资本论`

来源及支持范围：

- [马恩全集第四十四卷第六章 不变资本和可变资本](https://www.marxists.org/chinese/marx-engels-2/44/012.htm)：原典中文标题及正文直接使用“不变资本”。（high；真实搜索结果摘要；未逐页核读全文）

### 53. Kostpreis → 成本价格

ID：`kostpreis`；状态：**限语境或变体需处理**；建议译法：成本价格。

注释给出Kostenpreis/Kostpreis的中文“费用价格”，并分析多种含义；INKRIT词条明确列“成本价格”。译名存在语境差异。

实际查询：

- `Kostpreis Kostenpreis 成本价格 资本论 中文`

来源及支持范围：

- [马恩全集第二十六卷第一册第三章 亚当·斯密](https://www.marxists.org/chinese/marx-engels/26-1/005.htm)：注释给出Kostenpreis/Kostpreis的中文“费用价格”，并分析多种含义；INKRIT词条明确列“成本价格”。译名存在语境差异。（high；真实搜索结果摘要；未逐页核读全文）

### 54. Kreditkrise → 信用危机

ID：`kreditkrise`；状态：**已确认**；建议译法：信用危机。

HKWM多语词条明确给出信用危机，属于马克思主义语境的直接德中对照。

实际查询：

- `Kreditkrise 德语 信用危机 马克思 中文`
- `"Kreditkrise" "信用危机"`

来源及支持范围：

- [Kreditkrise — Historisch-kritisches Wörterbuch des Marxismus](https://inkrit.de/neuinkrit/e_inkritpedia/e_maincode/doku.php?id=k%3Akreditkrise)：词条中文栏列信用危机。（马克思主义专业辞典；搜索结果摘要；未逐页全文核读）

### 55. Kreditsystem → 信用制度

ID：`kreditsystem`；状态：**限语境或变体需处理**；建议译法：信用制度。

搜索结果摘要直接使用“信用制度”讨论马克思《资本论》中的信用制度理论。

实际查询：

- `Kreditsystem 信用制度 资本论 中文 马克思`

来源及支持范围：

- [东岳论丛：马克思信用制度理论](https://dylc.cbpt.cnki.net/portal/journal/portal/client/paper/a54d27269ac18354025317b5873a55aa)：搜索结果摘要直接使用“信用制度”讨论马克思《资本论》中的信用制度理论。（medium；真实搜索结果摘要；未逐页核读全文）

### 56. Krisentheorie → 危机理论

ID：`krisentheorie`；状态：**限语境或变体需处理**；建议译法：危机理论。

中文译本标题直接使用“危机理论”，并讨论马克思主义危机理论。

实际查询：

- `Krisentheorie 危机理论 马克思 中文 学术`

来源及支持范围：

- [第一章 导言：马克思主义和危机理论](https://www.marxists.org/chinese/reference-books/simon-clarke-1994/01.htm)：中文译本标题直接使用“危机理论”，并讨论马克思主义危机理论。（medium；真实搜索结果摘要；未逐页核读全文）

### 57. Kritik → 批判

ID：`kritik`；状态：**限语境或变体需处理**；建议译法：批判。

批判 is established for Kritik. Keep the Young Hegelian labels 批判的批判 and 纯粹批判 as context-specific phrases rather than variants that replace the base term.

现有变体：kritische Kritik；reine Kritik。

变体意见：kritische Kritik为批判的批判；reine Kritik为纯粹批判，不宜都退化为批判。

实际查询：

- `Marx Kritik kritische Kritik reine Kritik 批判的批判 德意志意识形态 中文 德文`

来源及支持范围：

- [法兰克福学派——从“马克思咖啡厅”到“马克斯咖啡厅”](https://ptext.nju.edu.cn/info/1348/9166.htm)：The Chinese academic text explicitly says Kritik is conventionally translated as 批判 and discusses its range of meanings. The German Ideology variants kritische Kritik and reine Kritik are author-specific polemical phrases, not alternate headwords for the ordinary noun.（search_snippet；Search result snippet; page body not independently inspected.）
- [马克思恩格斯全集第二卷：批判的批判的通讯](https://www.marxists.org/chinese/marx-engels/02/008.htm)：《神圣家族》相关章标题与正文使用批判的批判，支持青年黑格尔派论战的特定表述。（search_snippet；Search result snippet.）

### 58. Kritik der politischen Ökonomie → 政治经济学批判

ID：`kritik-der-politischen-okonomie`；状态：**已确认**；建议译法：政治经济学批判。

The German–Chinese glossary entry and the standard Chinese title of Marx’s work support the existing target.

实际查询：

- `Kritik der politischen Ökonomie 政治经济学批判 德文 中文 马克思`

来源及支持范围：

- [Inkrit-Pedia: Kritik der politischen Ökonomie](https://www.inkrit.de/e_inkritpedia/e_maincode/doku.php?id=k%3Akritik_der_politischen_oekonomie)：The German headword is listed with the Chinese equivalent 政治经济学批判.（multilingual_glossary；Search result snippet; page body not independently inspected.）
- [马克思恩格斯文集：Kritik der politischen Ökonomie（德文书名页）](https://www.marxists.org/chinese/pdf/marx-engels/me2/me2-44.pdf)：Search snippet shows the German title on the Marx/Engels text and corroborates the exact German phrase; the Chinese project title is 政治经济学批判.（bibliographic_cross_language；Search result snippet.）

### 59. Lohnarbeit → 雇佣劳动

ID：`lohnarbeit`；状态：**已确认**；建议译法：雇佣劳动。

Lexical and canonical-title evidence support 雇佣劳动.

实际查询：

- `Lohnarbeit 雇佣劳动 德意志意识形态 中文 Marx 德文`

来源及支持范围：

- [Inkrit-Pedia: Lohnarbeit](https://www.inkrit.de/e_inkritpedia/e_maincode/doku.php?id=l%3Alohnarbeit)：The multilingual lexicon explicitly lists C: gùyōng láodòng 雇佣劳动 for the German headword Lohnarbeit.（multilingual_glossary；Search result snippet; page body not independently inspected.）
- [马克思：《雇佣劳动与资本》](https://www.marxists.org/chinese/marx-engels/mia-chinese-marx-1847-1849.htm)：The canonical Chinese title uses 雇佣劳动 for the corresponding Marxian concept; the German title is Lohnarbeit und Kapital.（work_title_cross_language；Search result snippet.）

### 60. materialistische Geschichtsauffassung → 唯物主义历史观

ID：`materialistische-geschichtsauffassung`；状态：**限语境或变体需处理**；建议译法：唯物主义历史观。

The Chinese convention is well supported, but sources distinguish related expressions such as 唯物史观 and 历史唯物主义; retain the baseline wording for this German phrase.

实际查询：

- `materialistische Geschichtsauffassung 唯物主义历史观 Marx 中文 德文`

来源及支持范围：

- [张秀琴：历史唯物主义的历史概念](https://marx.ruc.edu.cn/sy/mydj/5f137a579372480cad7f7607ea8b0605.htm)：The Chinese scholarly source distinguishes Marx’s and Engels’s historical terminology and explicitly connects materialistische Geschichtsauffassung with 唯物主义历史观/唯物史观 usage.（search_snippet；Search result snippet; page body not independently inspected.）
- [以历史唯物主义为基础的马克思主义社会学](https://src.ruc.edu.cn/CN/article/downloadArticleFile.do?attachType=PDF&id=347)：The paper discusses the German phrase materialistische Geschichtsauffassung and its Chinese rendering in the context of Marxist historiography.（search_snippet；Search result snippet.）

### 61. Mehrarbeit → 剩余劳动

ID：`mehrarbeit`；状态：**已确认**；建议译法：剩余劳动。

Primary translation and multilingual glossary directly support the established equivalent.

实际查询：

- `Mehrarbeit 剩余劳动 Marx 德文 中文 资本论`

来源及支持范围：

- [Inkrit-Pedia: Mehrarbeit](https://www.inkrit.de/e_inkritpedia/e_maincode/doku.php?id=m%3Amehrarbeit)：Multilingual entry explicitly gives Chinese 剩余劳动 for Mehrarbeit.（multilingual_glossary；Search result snippet; page body not independently inspected.）
- [《资本论》第一卷第三篇：绝对剩余价值的生产](https://www.diancangwang.cn/waiguomingzhu/ac82405c9cbe/adbbb172fb7c.html)：Chinese passage explicitly labels Mehrarbeit as 剩余劳动, with the German term shown in parentheses.（primary_translation_with_german；Search result snippet.）

### 62. Mehrwert → 剩余价值

ID：`mehrwert`；状态：**已确认**；建议译法：剩余价值。

Standard Marxian translation is directly supported by dictionary and primary-text context.

实际查询：

- `Mehrwert 剩余价值 Marx 德文 中文 资本论`

来源及支持范围：

- [德语助手：Mehrwert 德汉词典](https://www.godic.net/dicts/de/Mehrwert)：The economics sense is translated as 剩余价值.（dictionary_entry；Search result snippet.）
- [《反杜林论》第二编：政治经济学](https://www.marxists.org/chinese/marx-engels-2/26/004.htm)：Chinese Marx/Engels text identifies the increment Marx calls Mehrwert as 剩余价值; supports the standard Marxian usage.（primary_translation_context；Search result snippet.）

### 63. Mehrwertmasse → 剩余价值量

ID：`mehrwertmasse`；状态：**限语境或变体需处理**；建议译法：剩余价值量。

《资本论》第一卷第九章中德对应标题和相同计算关系，支持剩余价值量。德文标题采用Masse des Mehrwerts而非合成词Mehrwertmasse，因此明确记录为同概念的组合形式对应。

实际查询：

- `Mehrwertmasse 剩余价值量 德文 中文 马克思`
- `Marx Kapital "Rate und Masse des Mehrwerts" Mehrwertmasse`
- `"Mehrwertmasse" "剩余价值量"`
- `"剩余价值率和剩余价值量" 《资本论》`

来源及支持范围：

- [Das Kapital I — Rate und Masse des Mehrwerts](https://biancahoegel.com/archiv/marx_engels/kapital/bd1/me23_321.htm)：第九章定义Masse des Mehrwerts，等于可变资本量与剩余价值率之积。（经典德文原文；搜索结果摘要；未逐页全文核读）
- [第九章：剩余价值率和剩余价值量](https://www.marxists.org/chinese/marx/capital/09.htm)：对应章节标题及相同计算关系使用剩余价值量。（经典中译；搜索结果摘要；未逐页全文核读）

### 64. Mehrwertrate → 剩余价值率

ID：`mehrwertrate`；状态：**已确认**；建议译法：剩余价值率。

The lexical entry directly pairs the German term with the Chinese target and the primary text confirms the m/v concept.

实际查询：

- `Mehrwertrate 剩余价值率 德文 中文 m v`

来源及支持范围：

- [Inkrit-Pedia: Mehrwertrate](https://www.inkrit.de/e_inkritpedia/e_maincode/doku.php?id=m%3Amehrwertrate)：Multilingual entry gives 剩余价值率 for Mehrwertrate and states the m/v formula.（multilingual_glossary；Search result snippet.）
- [《资本论》第三卷：剩余价值率与利润率](https://marx-zh-cn.github.io/docs/MEW-ZH/25/MEW25-004.html)：Chinese translation calls the ratio m/v 剩余价值率.（primary_translation_context；Search result snippet; German corresponding text not independently inspected.）

### 65. menschliche Emanzipation → 人的解放

ID：`menschliche-emanzipation`；状态：**限语境或变体需处理**；建议译法：人的解放。

The existing Chinese term is conventional; evidence connects the German expression and Chinese concept across separate sources rather than one bilingual translation.

实际查询：

- `menschliche Emanzipation 人的解放 Marx 德文 中文`

来源及支持范围：

- [Die Freiheit im Lichte des Marxismus-Leninismus](https://www.bpb.de/shop/zeitschriften/apuz/archiv/525498/die-freiheit-im-lichtedes-marxismus-leninismus/)：German source uses menschliche Emanzipation in the Marx discussion; Chinese Marxist scholarship uses 人的解放 for the associated emancipatory concept. Cross-source conceptual evidence, not a bilingual edition pair.（contextual_cross_source；Search result snippets only.）
- [人的解放问题与唯物主义历史观](https://marxism.ssap.com.cn/skwx_marxism/LiteratureDetail.aspx?id=121707)：Chinese scholarly source defines the Marxian concept as 人的解放.（scholarly_context；Search result snippet.）

### 66. monetäre Werttheorie → 货币价值论

ID：`monetare-werttheorie`；状态：**限语境或变体需处理**；建议译法：货币价值论。

The translation is supported as a field-specific label in New Marx Reading scholarship; do not generalize it to all uses of Werttheorie.

实际查询：

- `monetäre Werttheorie 货币价值论 Neue Marx-Lektüre 德文 中文`

来源及支持范围：

- [李乾坤：马克思价值形式理论研究的历史演进与当代展望](https://ptext.nju.edu.cn/info/1348/9207.htm)：Root-verified NJU source discusses the monetary value-theory debate and the Neue Marx-Lektüre context; supports 货币价值论 as the Chinese label in this scholarly usage.（scholarly_author_specific；Opened and inspected by parent agent; scholarly usage, not a general dictionary entry.）
- [Michael Wendl: Geldtheorie und monetäre Werttheorie von Marx im Unterschied zu Keynes](https://www.metropolis-verlag.de/Geldtheorie-und-monetaere-Werttheorie-von-Marx-im-Unterschied-zu-Keynes/14128/book.do)：German publication title confirms the exact German term and its Marx-theory context.（bibliographic_context；Search result snippet.）

### 67. Mystifikation → 神秘化

ID：`mystifikation`；状态：**已确认**；建议译法：神秘化。

Both a multilingual terminology entry and Chinese Marx scholarship support 神秘化.

实际查询：

- `Mystifikation 神秘化 Marx 资本论 德文 中文`

来源及支持范围：

- [Inkrit-Pedia: Mystifikation](https://inkrit.de/e_inkritpedia/e_maincode/doku.php?id=m%3Amystifikation)：Multilingual entry explicitly lists the Chinese equivalents 神秘性 and 神秘化 for Mystifikation; the latter matches the baseline target.（multilingual_glossary；Search result snippet; page body not independently inspected.）
- [马克思的哲学和实践](https://ptextold.nju.edu.cn/b8/74/c12208a243828/page.htm)：Chinese scholarly source names Mystification/Mystifikation among Marx’s Capital concepts and gives 神秘化.（scholarly_context；Search result snippet.）

### 68. notwendige Arbeit → 必要劳动

ID：`notwendige-arbeit`；状态：**限语境或变体需处理**；建议译法：必要劳动。

The target is standard in Marxian Chinese usage, but this search did not locate a direct German–Chinese pairing for the exact phrase notwendige Arbeit.

实际查询：

- `notwendige Arbeit 必要劳动 Marx necessary labour 德文 资本论 中文`

来源及支持范围：

- [马克思：《必要劳动和剩余劳动（剩余产品）》](https://www.marxists.org/chinese/marx-engels2/08/28.htm)：Chinese title and text use 必要劳动 as the paired Marxian category; search did not expose the corresponding German headword in this excerpt.（primary_translation_context；Search result snippet.）
- [马克思政治经济学的哲学性质问题论纲](https://www.cssn.cn/zx/mkszyzx/202210/t20221027_5555189.shtml)：Chinese scholarly discussion uses 必要劳动 in the Marxian distinction from surplus labour.（scholarly_context；Search result snippet.）

### 69. organische Zusammensetzung des Kapitals → 资本有机构成

ID：`organische-zusammensetzung-des-kapitals`；状态：**已确认**；建议译法：资本有机构成。

Chinese terminology standard, Chinese Capital chapter, and German original support the equivalent and the technical definition.

现有变体：organische Zusammensetzung。

变体意见：{'organische Zusammensetzung': 'The shortened form is acceptable only where the capital-composition topic is explicit; otherwise retain the full source phrase.'}

实际查询：

- `organische Zusammensetzung des Kapitals 资本有机构成 Wertzusammensetzung technical composition 中文 德文`

来源及支持范围：

- [全国科学技术名词审定委员会：经济学名词（资本构成术语）](https://www.cnterm.cn/news/tzgg/202503/P020250909521438793386.pdf)：该征求意见稿提供中文及英文概念对照，仅作辅助；德文对应另由经典原文核对。（全国科技名词委征求意见稿（非正式审定标准）；Search result snippet; PDF body not independently inspected.）
- [《资本论》第一卷第二十三章：资本主义积累的一般规律](https://www.marxists.org/chinese/marx/capital/23.htm)：Chinese chapter discusses the relation between technical composition and value composition of capital, the defining context for organic composition.（primary_translation_context；Search result snippet.）
- [Das Kapital, Band I, Kapitel 23: Das allgemeine Gesetz der kapitalistischen Akkumulation](https://marx-ebooks.github.io/docs/MEW-ZENO/23/ME23-640.html)：German text names the value-composition determined by technical composition organische Zusammensetzung des Kapitals.（primary_source；Search result snippet.）

### 70. politische Emanzipation → 政治解放

ID：`politische-emanzipation`；状态：**已确认**；建议译法：政治解放。

The Chinese and German versions of Zur Judenfrage establish the term directly; Marx distinguishes it from full human emancipation.

实际查询：

- `politische Emanzipation 政治解放 Marx 中文 德文 Zur Judenfrage`

来源及支持范围：

- [马克思：《论犹太人问题》](https://www.marxists.org/chinese/marx-engels2/01/02.htm)：Chinese translation uses 政治解放 for Marx’s political emancipation concept and contrasts it with 人的解放.（primary_translation；Search result snippet.）
- [Karl Marx: Zur Judenfrage](https://de.wikisource.org/wiki/Zur_Judenfrage)：German primary text explicitly uses die politische Emancipation and staatsbürgerliche, die politische Emancipation.（primary_source；Search result snippet.）

### 71. Praxis → 实践

ID：`praxis`；状态：**限语境或变体需处理**；建议译法：实践。

Praxis名词的中文术语为“实践”；《马克思主义思想辞典》及高校研究直接使用“实践（Praxis）”。

现有变体：praktisch。

变体意见：praktisch为形容词或副词，需按句法处理，不能无条件套实践。

实际查询：

- `Marx Praxis 实践 praktisch 实践的 德意志意识形态 中文`
- `Praxis 实践 Marx 德意志意识形态 术语`

来源及支持范围：

- [马克思主义思想辞典：P](https://www.marxists.org/chinese/dictionary-of-marxism/marxist.org-chinese-dictionary-of-marxism-P.htm)：词条标题直接为“实践（praxis）”，并讨论马克思的实践概念。（high；实际搜索结果摘要，未逐页核读全文）
- [《现代哲学》2015年第4期](https://mphilosophy.sysu.edu.cn/sites/default/files/2020-06/%E7%8E%B0%E4%BB%A3%E5%93%B2%E5%AD%A62015%E5%B9%B4%E7%AC%AC4%E6%9C%9F.pdf)：文中明确区分“实践”（Praxis）和“实践的/地”（praktisch）。（high；实际搜索结果摘要，未逐页核读全文）

### 72. Privateigentum → 私有制

ID：`privateigentum`；状态：**限语境或变体需处理**；建议译法：私有制。

Privateigentum可译“私有财产”或“私有制”，中文经典文本在制度语境中用“私有制”；Eigentum本身可指财产、所有权或所有制，不能作为Privateigentum无条件变体。

现有变体：Eigentum。

变体意见：Eigentum是财产、所有权或所有制，不必然含私人限定，不应作为私有制的等价变体。

实际查询：

- `Privateigentum 私有制 Eigentum 所有制 马克思 中文 德意志意识形态`

来源及支持范围：

- [中国人民大学哲学院：马克思恩格斯对Eigentum的语境用法](https://phi.ruc.edu.cn/xsyj/sxwk/f64fda797bae42a287dc3dea57db99fc.htm)：讨论Eigentum可按语境译财产、所有制，并引用《德意志意识形态》中的私有制用例。（high；实际搜索结果摘要，未逐页核读全文）
- [《共产党宣言》中文文本](https://www.marxists.org/chinese/marx/01.htm)：中文经典文本使用“私有财产”“私有制”等表达；显示需按语境区分property与制度概念。（high；实际搜索结果摘要，未逐页核读全文）

### 73. Produktionskostentheorie → 生产费用论

ID：`produktionskostentheorie`；状态：**证据不足**；建议译法：生产费用论。

检索到德文经济学辞典对Produktionskostentheorie的定义，但本轮未找到足以确认中文固定译名“生产费用论”的可靠中文术语来源，暂标unverified。

实际查询：

- `Produktionskostentheorie 生产费用论 生产费用价值理论 Marx 中文`
- `production cost theory Marx 生产费用论 政治经济学`

来源及支持范围：

- [Gabler Wirtschaftslexikon：Produktionskostentheorie](https://wirtschaftslexikon.gabler.de/definition/produktionskostentheorie-46900)：德文辞典定义其为以工资、资本利息、地租等生产成本解释长期均衡价格的价值理论；不提供中文固定译名。（high；实际搜索结果摘要，未逐页核读全文）

### 74. Produktionskräfte → 生产力

ID：`produktionskrafte`；状态：**限语境或变体需处理**；建议译法：生产力。

中文研究和马克思主义词典常将Produktivkräfte译为“生产力”；Produktionskräfte/Produktionskraft也见于早期或不同语法形式，需区分单数、复数及历史用法。

现有变体：Produktivkräfte。

变体意见：['Produktivkräfte是固定常见复数形式；Produktionskraft/Produktionskräfte形式不应扩大为所有“生产力”词形的无差别替换。']

实际查询：

- `Produktionskräfte 生产力 Produktivkräfte 德意志意识形态 中文`
- `Produktivkräfte 生产力 Marx 德文 中文 术语`

来源及支持范围：

- [《马克思主义思想辞典》相关篇章](https://www.marxists.org/chinese/pdf/philosophy/110404.pdf)：中文词条说明马克思通常将Produktivkräfte译为“生产力”。（high；实际搜索结果摘要，未逐页核读全文）
- [学术月刊：马克思生产力概念考察](https://xsyk.cbpt.cnki.net/portal/journal/portal/client/paper/a07a2f1d1da0fcde102820350c4e5bab)：论文区分早期Produktionskraft与《德意志意识形态》中的Produktivkräfte概念用法。（high；实际搜索结果摘要，未逐页核读全文）

### 75. Produktionspreis → 生产价格

ID：`produktionspreis`；状态：**已确认**；建议译法：生产价格。

术语译名与《资本论》中文译本及学术文献一致；其范畴关系是成本价格加平均利润。

实际查询：

- `Produktionspreis 生产价格 成本价格 平均利润 资本论 第三卷`
- `马克思 生产价格 成本价格 平均利润 中文`

来源及支持范围：

- [《资本论》第三卷：生产价格](https://www.marxists.org/chinese/marx-engels/25/010.htm)：该章阐释生产价格由成本价格加平均利润构成，直接支持“生产价格”的概念对应。（high；搜索结果摘要，未逐页核读全文）

### 76. Produktionsverhältnisse → 生产关系

ID：`produktionsverhaltnisse`；状态：**限语境或变体需处理**；建议译法：生产关系。

“生产关系”是该马克思主义经济学范畴的惯用译名；须结合语境区分相关但范围不同的交往关系、交往形式。

实际查询：

- `Produktionsverhältnisse 生产关系 马克思 德意志意识形态 中文术语`
- `生产关系 马克思主义术语词典 Produktionsverhältnisse`

来源及支持范围：

- [马克思主义术语词典：生产关系](https://www.marxdic.com/article/2199)：词条说明马克思文本中的生产关系概念及中文术语用法；支持“生产关系”作为标准译名。（high；搜索结果摘要，未逐页核读全文）

### 77. Produktionsweise → 生产方式

ID：`produktionsweise`；状态：**已确认**；建议译法：生产方式。

中文研究将Produktionsweise作为“生产方式”讨论，其概念涉及物质生产的方式及生产力、生产关系。

实际查询：

- `Produktionsweise 生产方式 德意志意识形态 南京大学`
- `马克思 Produktionsweise 中文 生产方式 核心范畴`

来源及支持范围：

- [周嘉昕：历史与文本中的马克思“生产方式”概念](https://ptextold.nju.edu.cn/d9/aa/c13379a514474/page.htm)：中文学术文章讨论《德意志意识形态》的生产方式范畴，直接印证中文用语“生产方式”。（high；主代理已打开正文，核对文章标题和生产方式论述）

### 78. Profit → 利润

ID：`profit`；状态：**已确认**；建议译法：利润。

在马克思政治经济学语境中Profit译为“利润”；不能与Mehrwert（剩余价值）直接等同，利润是剩余价值的转化表现。

实际查询：

- `Profit 利润 马克思 剩余价值 利润概念 中文`
- `《资本论》第三卷 利润 剩余价值 利润率 马克思 中文`

来源及支持范围：

- [《资本论》第三卷第二章：利润率](https://www.marxists.org/chinese/marx-engels/25/003.htm)：正文区分利润与剩余价值，并以总资本为分母定义利润率。（high；主代理已打开正文，核对第6—10行）

### 79. Profitrate → 利润率

ID：`profitrate`；状态：**已确认**；建议译法：利润率。

Profitrate意为利润率；马克思分析中常以剩余价值/预付总资本表示，须区别Mehrwertrate（剩余价值率）。

实际查询：

- `Profitrate 利润率 马克思 资本论 第三卷`
- `Marx Profitrate Verhältnis Mehrwert Gesamtkapital 中文 利润率`

来源及支持范围：

- [《资本论》第三卷第二章：利润率](https://marxists.org/chinese/marx-engels/25/003.htm)：正文区分利润与剩余价值，并以总资本为分母定义利润率。（high；主代理已打开正文，核对第6—10行）

### 80. Proletariat → 无产阶级

ID：`proletariat`；状态：**限语境或变体需处理**；建议译法：无产阶级。

Proletariat指无产者构成的阶级；Proletarier指个体无产者，二者不得作为无差别变体替换。

现有变体：Proletarier。

变体意见：Proletarier是无产者，Proletariat是无产阶级，个体与阶级需区分。

实际查询：

- `Proletariat 无产阶级 Proletarier 无产者 区别 中文`
- `Proletarier 无产者 Proletariat 无产阶级 译名`

来源及支持范围：

- [北京大学马克思主义学院：无产阶级概念](https://marxism.pku.edu.cn/xzlt/9mkszyxy1294262.htm)：文章直接将Proletariat对应为“无产阶级”，并说明这是阶级概念。（high；搜索结果摘要，未逐页核读全文）
- [四川商务职业学院：‘全世界无产者，联合起来！’的历史出场及汉译演变](https://www.scsw.edu.cn/szb/info/1041/4117.htm)：文章区分Proletarier所指的个体“无产者”与Proletariat所指的群体/阶级“无产阶级”。（high；搜索结果摘要，未逐页核读全文）

### 81. reelle Subsumtion → 实际隶属

ID：`reelle-subsumtion`；状态：**建议修改**；建议译法：实际从属。

建议首选实际从属，以与形式从属成对；当前实际隶属可理解，但本轮核到的中文研究直接采用实际从属。此项为术语一致性建议。

实际查询：

- `reelle Subsumtion 实际从属 南京大学 马克思`

来源及支持范围：

- [南京大学：实际从属（reelle Subsumtion）相关研究](https://ptextold.nju.edu.cn/57/05/c13164a612101/page.htm)：来源直接以“实际从属”对应reelle Subsumtion；比“实际隶属”更符合该概念在中文马克思研究中的术语用法。（high；主代理提供来源，页面内容摘要/对应术语明确）

### 82. relative Wertform → 相对价值形式

ID：`relative-wertform`；状态：**已确认**；建议译法：相对价值形式。

该术语的经典中文译名稳定为“相对价值形式”，与等价形式构成价值表现的两极。

实际查询：

- `relative Wertform 相对价值形式 资本论 中文`
- `relative value-form 相对价值形式 马克思 术语词典`

来源及支持范围：

- [《资本论》第一卷：商品的相对价值形式](https://www.marxists.org/chinese/marx/capital/01.htm)：中文经典文本使用“相对价值形式”并解释一种商品如何在另一商品中表现其价值。（high；搜索结果摘要，未逐页核读全文）
- [马克思主义大辞典：相对价值形式](https://www.marxdic.com/article/2277)：词条标题直接为“相对价值形式（relative value form）”，对应术语明确。（high；搜索结果摘要，未逐页核读全文）

### 83. relativer Mehrwert → 相对剩余价值

ID：`relativer-mehrwert`；状态：**已确认**；建议译法：相对剩余价值。

relativer Mehrwert译为“相对剩余价值”，指通过降低劳动力价值、缩短必要劳动时间而增加的剩余价值；与绝对剩余价值区分。

实际查询：

- `relativer Mehrwert 相对剩余价值 马克思 中文`
- `relativer Mehrwert relative surplus value 资本论 中文 术语`

来源及支持范围：

- [《资本论》第一卷：相对剩余价值的概念](https://www.marxists.org/chinese/marx/capital/10.htm)：中文经典文本明确定义：通过缩短必要劳动时间、改变工作日中必要劳动与剩余劳动的比例所生产的剩余价值称为相对剩余价值。（high；搜索结果摘要，未逐页核读全文）
- [《马克思恩格斯全集》第四十四卷：相对剩余价值的概念](https://www.marxists.org/chinese/marx-engels-2/44/016.htm)：卷本章节标题及正文采用“相对剩余价值”，与现有目标译名一致。（high；搜索结果摘要，未逐页核读全文）

### 84. Schatzbildung → 货币贮藏

ID：`schatzbildung`；状态：**限语境或变体需处理**；建议译法：货币贮藏。

Schatzbildung指货币退出流通并作为价值贮藏的过程；中文资本论文本采用‘货币贮藏’等表述。若语境指单纯财富积累，需结合上下文决定是否简化为‘贮藏’。

实际查询：

- `Schatzbildung 贮藏货币 货币贮藏 马克思 资本论`
- `Schatzbildung Marx Kapital 中文 货币贮藏 贮藏货币`

来源及支持范围：

- [《资本论》第一卷：货币或商品流通](https://www.marxists.org/chinese/marx/capital/03.htm)：中文文本讨论货币作为贮藏货币及货币贮藏在金属流通中的职能，直接支持‘货币贮藏’作为概念译法。（high；搜索结果摘要，未逐页核读全文）
- [《资本论》第一卷：商品与货币（含德文术语对照）](https://www.diancangwang.cn/waiguomingzhu/ac82405c9cbe/909b5d49b4e0.html)：文本将货币凝固为贮藏货币的过程与Schatzbildner关联，提供Schatz词族对应的中文语境。（medium；搜索结果摘要，未逐页核读全文）

### 85. Schein → 假象

ID：`schein`；状态：**已确认**；建议译法：假象。

Source distinguishes Schein from Wesen/Erscheinung and gives 假象 in the Chinese discussion.

实际查询：

- `Marx Schein Erscheinung Wesen 假象 表象 中文 资本论 德文`

来源及支持范围：

- [《现代哲学》2019年第3期：本质、显现与假象](https://mphilosophy.sysu.edu.cn/sites/default/files/2020-07/现代哲学2019年第3期.pdf)：Source distinguishes Schein from Wesen/Erscheinung and gives 假象 in the Chinese discussion.（search_snippet；Search result snippet; page body not independently inspected.）

### 86. Scheinausgleich → 表面调和

ID：`scheinausgleich`；状态：**证据不足**；建议译法：表面调和。

Two targeted searches returned no stable German–Chinese equivalent; lexical gloss alone is insufficient.

实际查询：

- `Scheinausgleich Marx Scheinausgleich 中文 表面调和 德文`
- `Scheinausgleich Marx Ausgleich 中文 调和 资本主义 德文`

来源及支持范围：

- 本轮未取得可采信的直接来源。

### 87. Selbstbetätigung → 自主活动

ID：`selbstbetatigung`；状态：**限语境或变体需处理**；建议译法：自主活动。

Nanjing University discussion directly associates both German terms with 自主活动; near-synonymy does not establish full interchangeability.

现有变体：Selbsttätigkeit。

变体意见：{'Selbsttätigkeit': 'Search evidence does not establish unconditional interchangeability; preserve contextual distinction.'}

实际查询：

- `Selbstbetätigung Selbsttätigkeit 德国意识形态 自主活动 中文 原文`

来源及支持范围：

- [张一兵：《人的自主活动力量颠倒地表现为事物的力量——广松涉版〈德意志意识形态〉“费尔巴哈章”新探》](https://ptextold.nju.edu.cn/e1/5e/c27389a581982/page.htm)：Page body defines 自主活动 and discusses it in the German Ideology; it supports 自主活动 for Selbstbetätigung, but does not independently establish that Selbsttätigkeit is interchangeable.（page_body；Opened source; title and abstract inspected.）

### 88. sinnliche Tätigkeit → 感性活动

ID：`sinnliche-tatigkeit`；状态：**已确认**；建议译法：感性活动。

Chinese Marx/Engels text and scholarship use 感性活动 for sinnliche Tätigkeit.

实际查询：

- `sinnliche Tätigkeit 感性活动 德意志意识形态 中文 马克思`

来源及支持范围：

- [《德意志意识形态》中文本（马克思恩格斯文集第1卷）](https://www.marxists.org/chinese/pdf/marx-engels/mes01.pdf)：Chinese Marx/Engels text and scholarship use 感性活动 for sinnliche Tätigkeit.（search_snippet；Search result snippet; page body not independently inspected.）

### 89. subjektive Arbeitswertlehre → 主观劳动价值论

ID：`subjektive-arbeitswertlehre`；状态：**证据不足**；建议译法：主观劳动价值论。

海因里希著作德文目录确有Subjektive Arbeitswertlehre这一标题，但本轮未找到足以确认主观劳动价值论为通行中译的可靠中文材料。

实际查询：

- `subjektive Arbeitswertlehre 中文 主观劳动价值论 马克思`
- `主观劳动价值论 Arbeitswertlehre Marx 德文 中文`

来源及支持范围：

- [Michael Heinrich: Die Wissenschaft vom Wert — Inhaltsverzeichnis](https://external.dandelon.com/download/attachments/dandelon/ids/AT001A74E9CC7C6F86007C1257E1800441172.pdf)：目录中有Subjektive Arbeitswertlehre und Produktionskostentheorie bei Adam Smith；仅证明德文术语存在，不能单独证明中译通行。（德文著作目录；搜索结果摘要；未逐页全文核读）

### 90. Tauschwert → 交换价值

ID：`tauschwert`；状态：**已确认**；建议译法：交换价值。

Chinese Capital text supports 交换价值.

实际查询：

- `Tauschwert 交换价值 Marx 德文 资本论 中文`

来源及支持范围：

- [《资本论》第一卷第一章：商品](https://www.marxists.org/chinese/marx/capital/01.htm)：Chinese Capital text supports 交换价值.（search_snippet；Search result snippet; page body not independently inspected.）

### 91. technische Zusammensetzung → 技术构成

ID：`technische-zusammensetzung`；状态：**限语境或变体需处理**；建议译法：技术构成。

Capital chapter on composition supports 技术构成 in the compound technical composition of capital.

实际查询：

- `technische Zusammensetzung des Kapitals 技术构成 马克思 中文 德文`

来源及支持范围：

- [《资本论》第一卷第二十三章：资本主义积累的一般规律](https://www.marxists.org/chinese/marx/capital/23.htm)：Capital chapter on composition supports 技术构成 in the compound technical composition of capital.（search_snippet；Search result snippet; page body not independently inspected.）

### 92. Teilung der Arbeit → 分工

ID：`teilung-der-arbeit`；状态：**限语境或变体需处理**；建议译法：分工。

Chinese German-Ideology translation supports 分工; variants are related forms.

现有变体：Arbeitsteilung。

变体意见：{'Arbeitsteilung': 'Search evidence does not establish unconditional interchangeability; preserve contextual distinction.'}

实际查询：

- `Teilung der Arbeit Arbeitsteilung 分工 德意志意识形态 中文`

来源及支持范围：

- [《德意志意识形态》中文本](https://www.marxists.org/chinese/pdf/marx-engels/mea01.pdf)：Chinese German-Ideology translation supports 分工; variants are related forms.（search_snippet；Search result snippet; page body not independently inspected.）

### 93. theoretisches Feld → 理论场域

ID：`theoretisches-feld`；状态：**证据不足**；建议译法：理论场域。

Two targeted searches did not establish a stable German–Chinese match for this phrase.

实际查询：

- `"theoretisches Feld" Althusser 中文 理论场域 理论领域`
- `theoretisches Feld 理论领域 马克思 德文 中文`

来源及支持范围：

- 本轮未取得可采信的直接来源。

### 94. Transformationsproblem → 转型问题

ID：`transformationsproblem`；状态：**建议修改**；建议译法：转形问题。

Chinese political-economy scholarship uses 转形问题 for the value-to-production-price problem; 转型问题 is not preferred here.

实际查询：

- `Transformationsproblem Wert Produktionspreise 转形问题 价值 生产价格 上财`

来源及支持范围：

- [成本价格转形与转形问题C体系的特点](https://marxism.ssap.com.cn/skwx_marxism/LiteratureDetail.aspx?ID=38724)：Chinese political-economy scholarship uses 转形问题 for the value-to-production-price problem; 转型问题 is not preferred here.（search_snippet；Search result snippet; page body not independently inspected.）

### 95. Trinitarische Formel → 三位一体公式

ID：`trinitarische-formel`；状态：**已确认**；建议译法：三位一体公式。

The Chinese Chapter 48 title and German chapter title establish 三位一体的公式/三位一体公式 directly.

实际查询：

- `"Trinitarische Formel" 三位一体公式 马克思 资本论 中文`
- `Die trinitarische Formel 第四十八章 三位一体的公式 资本论 德文 中文`
- `Kapital 48 "Die trinitarische Formel" Marx deutsch`

来源及支持范围：

- [《资本论》第三卷第四十八章：三位一体的公式](https://www.marxists.org/chinese/marx-engels/25/049.htm)：Chinese Chapter 48 title is 三位一体的公式; German Chapter 48 title is Die trinitarische Formel. Same-chapter cross-language match.（same_chapter_cross_language；Search results identify the Chinese and German Chapter 48 titles.）
- [Das Kapital III — Die trinitarische Formel](https://www.biancahoegel.de/archiv/marx_engels/kapital/bd3/me25_822.htm)：德文第48章标题，与中文同章对应。（经典德文原文；搜索结果摘要；未逐页全文核读）

### 96. Träger → 承担者

ID：`trager`；状态：**已确认**；建议译法：承担者。

Chinese edition uses 承担者 in the relevant economic-relations phrasing.

实际查询：

- `Träger ökonomischer Verhältnisse 承担者 马克思 中文 资本论`

来源及支持范围：

- [诺曼·杰拉斯：《保卫人性》](https://www.marxists.org/chinese/reference-books/norman-geras-1983/04.htm)：Chinese edition uses 承担者 in the relevant economic-relations phrasing.（search_snippet；Search result snippet; page body not independently inspected.）

### 97. Umschlag der Aneignungsgesetze → 占有规律的转变

ID：`umschlag-der-aneignungsgesetze`；状态：**限语境或变体需处理**；建议译法：占有规律的转变。

The Capital chapter heading supports the Chinese concept; PROKLA confirms the German theoretical context. Exact sentence-level alignment remains unverified.

实际查询：

- `Umschlag der Aneignungsgesetze 马克思 占有规律 转化 中文`
- `Umschlag Aneignungsgesetze 原始积累 中文`

来源及支持范围：

- [《资本论》第一卷第二十二章：占有规律的转变](https://www.marxists.org/chinese/marx/capital/22.htm)：Chapter heading supports a Chinese rendering of the concept; exact German phrase alignment was not verified.（same_chapter_cross_language；Chapter-level cross-reference; exact passage not inspected.）
- [PROKLA: The transformation of the laws of appropriation](https://prokla.de/index.php/PROKLA/en/article/download/332/297/594)：German discussion treats the transformation of appropriation laws as concealing unpaid appropriation; this confirms context, not Chinese wording.（search_snippet；Search snippet; page body not independently inspected.）

### 98. ursprüngliche Akkumulation → 原始积累

ID：`ursprungliche-akkumulation`；状态：**已确认**；建议译法：原始积累。

Chinese title/translation supports 原始积累.

实际查询：

- `ursprüngliche Akkumulation 原始积累 德意志意识形态 资本论 中文`

来源及支持范围：

- [《资本论》第一卷第二十四章：所谓原始积累](https://www.marxists.org/chinese/marx-engels-2/44/030.htm)：Chinese title/translation supports 原始积累.（search_snippet；Search result snippet; page body not independently inspected.）

### 99. variables Kapital → 可变资本

ID：`variables-kapital`；状态：**已确认**；建议译法：可变资本。

Chinese Capital chapter supports 可变资本.

实际查询：

- `variables Kapital 可变资本 variable capital 中文 马克思 资本论`

来源及支持范围：

- [《资本论》第一卷第六章：不变资本和可变资本](https://www.marxists.org/chinese/marx/capital/06.htm)：Chinese Capital chapter supports 可变资本.（search_snippet；Search result snippet; page body not independently inspected.）

### 100. Verdinglichung → 物化

ID：`verdinglichung`；状态：**建议修改**；建议译法：物化。

物化对应Verdinglichung可保留；修改建议针对变体：Versachlichung应按研究语境另列物象化、事物化等译法，不宜作为无条件同义变体。

现有变体：Versachlichung。

变体意见：移除Versachlichung等价变体，另条记录其物象化、事物化等译法。

实际查询：

- `Verdinglichung Versachlichung 物化 事物化 Marx 中文 南大 清华`

来源及支持范围：

- [张义修：《马克思主义“物化”批判逻辑的再发现——基于对马克思、卢卡奇与法兰克福学派的概念史考察》](https://ptextold.nju.edu.cn/09/4f/c27389a461135/page.htm)：Page body explicitly differentiates Verdinglichung（物化） from Versachlichung（事物化） and warns against conflating them.（page_body；Opened source; title, abstract and terminology discussion inspected.）
- [马克思哲学论坛第三十六期举行](https://www.rwxy.tsinghua.edu.cn/info/1161/6473.htm)：报告明确区分物象化（Versachlichung）与物化（Verdinglichung）。（学术研究；已打开网页正文，核对第68—69行）

### 101. Verelendungstheorie → 贫困化理论

ID：`verelendungstheorie`；状态：**限语境或变体需处理**；建议译法：贫困化理论。

A bilingual terminology resource supports 贫困化理论; evidence is lexical and not an authoritative scholarly translation.

实际查询：

- `Verelendungstheorie 贫困化理论 马克思 中文 绝对 相对贫困`
- `Verelendungstheorie 中文 德文 马克思 贫困化`

来源及支持范围：

- [Verelendungstheorie（德汉词条）](https://www.verben.de/substantive/ressourcen/Verelendungstheorie.htm)：Bilingual terminology resource gives 贫困化理论; lexical evidence only, not a scholarly Marx-text translation.（dictionary_entry；Search snippet; page body not independently inspected.）

### 102. Verkehr → 交往

ID：`verkehr`；状态：**限语境或变体需处理**；建议译法：交往。

German–Chinese Marx text usage supports 交往 for Verkehr; compounds require separate handling.

现有变体：Verkehrsverhältnisse。

变体意见：Verkehrsverhältnisse应译交往关系，不能丢弃Verhältnisse。

实际查询：

- `Verkehr Verkehrsverhältnisse 交往 交往关系 德意志意识形态 中文`

来源及支持范围：

- [《国际新闻界》2025年第7期：Verkehr的翻译与马克思的传播观](https://cjjc.ruc.edu.cn/CN/Y2025/V47/I7/6)：The article directly discusses translating Verkehr as 交往 and distinguishes related compounds.（search_snippet；Search snippet; page body not independently inspected.）

### 103. Verkehrsform → 交往形式

ID：`verkehrsform`；状态：**限语境或变体需处理**；建议译法：交往形式。

Chinese scholarship explicitly pairs Verkehrsform with 交往形式; Form des Verkehrs is a reordered variant whose exact scope remains context-dependent.

现有变体：Form des Verkehrs。

变体意见：{'Form des Verkehrs': 'Search evidence does not establish unconditional interchangeability; preserve contextual distinction.'}

实际查询：

- `Verkehrsform 交往形式 Verkehrsform 德意志意识形态 中文 马克思`

来源及支持范围：

- [《国际新闻界》2024年第4期：马克思文本中的Verkehr概念](https://cjjc.ruc.edu.cn/CN/Y2024/V46/I4/6)：Chinese scholarship explicitly pairs Verkehrsform with 交往形式 and discusses the word-order variant Form des Verkehrs.（search_snippet；Search snippet; page body not independently inspected.）

### 104. Verwandlung von Geld in Kapital → 货币转化为资本

ID：`verwandlung-von-geld-in-kapital`；状态：**已确认**；建议译法：货币转化为资本。

Chinese Capital translation supports 货币转化为资本.

现有变体：Verwandlung des Geldes in Kapital。

变体意见：{'Verwandlung des Geldes in Kapital': 'Search evidence does not establish unconditional interchangeability; preserve contextual distinction.'}

实际查询：

- `Verwandlung von Geld in Kapital 货币转化为资本 德文 资本论`

来源及支持范围：

- [《资本论》第一卷第四章：货币转化为资本](https://www.marxists.org/chinese/marx/capital/04.htm)：Chinese chapter title supports 货币转化为资本; German Urtext title is Verwandlung des Geldes in Kapital.（same_chapter_cross_language；Chinese translation and German Urtext were separately identified in search results.）
- [Karl Marx: Urtext — Verwandlung des Geldes in Kapital](https://www.marxists.org/deutsch/archiv/marx-engels/1858/urtext/3.htm)：German text uses the same phrase for the transition to capital.（search_snippet；Search snippet; page body not independently inspected.）

### 105. Verwertung → 价值增殖

ID：`verwertung`；状态：**限语境或变体需处理**；建议译法：价值增殖。

Chinese scholarly discussion maps Selbstverwertung to 自行增殖; this does not make it a variant of Verwertung in every context.

现有变体：Selbstverwertung。

变体意见：Selbstverwertung强调自行增殖，需保留Selbst限定。

实际查询：

- `Verwertung Selbstverwertung 价值增殖 自我增殖 Marx 德文 中文`
- `Wertverwertung Selbstverwertung 自我增殖 价值增殖`

来源及支持范围：

- [《哲学研究》：资本的价值自行增殖过程](https://zxyj.cbpt.cnki.net/portal/journal/portal/client/paper/80072fd6cf20f22a6925fb805ede2df7)：Chinese scholarship gives Selbstverwertung as 自行增殖; supports distinguishing reflexive Selbstverwertung from bare Verwertung.（search_snippet；Search snippet; page body not independently inspected.）

### 106. Verwertungsprozeß → 价值增殖过程

ID：`verwertungsproze`；状态：**已确认**；建议译法：价值增殖过程。

学术论文直接对照Verwertungsprozeß与价值增殖过程；旧拼写ß不影响该词对应。

实际查询：

- `Verwertungsprozeß 价值增殖过程 Marx 德文 中文`
- `"Verwertungsprozeß" "价值增殖过程"`

来源及支持范围：

- [马克思“资本观”三题议（现代哲学2023年第6期）](https://mphilosophy.sysu.edu.cn/sites/default/files/2024-05/%E5%AE%9A%E7%A8%BF%20%E7%8E%B0%E4%BB%A3%E5%93%B2%E5%AD%A62023%E7%AC%AC6%E6%9C%9F.pdf)：论文并列劳动过程和价值增殖过程的德文Arbeitsprozeß und Verwertungsprozeß。（学术研究；搜索结果摘要；未逐页全文核读）

### 107. wahrer Sozialismus → 真正社会主义

ID：`wahrer-sozialismus`；状态：**已确认**；建议译法：真正社会主义。

《共产党宣言》第三部分中德相应段落支持真正的社会主义；这是特定思潮名称，应保留引号所表达的批判语气。

实际查询：

- `wahrer Sozialismus 真正社会主义 德意志意识形态 Marx 中文 德文`
- `"wahrer Sozialismus" site:marxists.org/deutsch/archiv/marx-engels/1848`
- `"真正的社会主义" site:marxists.org/chinese/marx/01.htm`

来源及支持范围：

- [Manifest der Kommunistischen Partei — Sozialistische und kommunistische Literatur](https://www.marxists.org/deutsch/archiv/marx-engels/1848/manifest/3-sozkomm.htm)：第三部分关于德国社会主义的段落出现wahrer Sozialismus。（经典原文；搜索结果摘要；未逐页全文核读）
- [马克思恩格斯：共产党宣言](https://www.marxists.org/chinese/marx/01.htm)：对应段落将该德国思潮译为真正的社会主义。（经典文本中译；搜索结果摘要；未逐页全文核读）

### 108. Warenfetisch → 商品拜物教

ID：`warenfetisch`；状态：**已确认**；建议译法：商品拜物教。

Chinese scholarship explicitly identifies Warenfetisch as 商品拜物教.

实际查询：

- `Warenfetisch Warenfetischismus 商品拜物教 Marx 德文 中文`

来源及支持范围：

- [Fetischismus及相关词在马克思著作中的话语变迁](https://mphilosophy.sysu.edu.cn/sites/default/files/2020-06/%E7%8E%B0%E4%BB%A3%E5%93%B2%E5%AD%A62017%E7%AC%AC1%E6%9C%9F.pdf)：论文将Warenfetisch直接对应商品拜物教，并讨论商品物神的译法。（学术研究；搜索结果摘要；未逐页全文核读）

### 109. Weltgeld → 世界货币

ID：`weltgeld`；状态：**限语境或变体需处理**；建议译法：世界货币。

Chinese Marxist economic scholarship uses 世界货币.

实际查询：

- `Weltgeld 世界货币 Marx 德文 中文 资本论`

来源及支持范围：

- [马克思的国际金融理论](https://www.imi.ruc.edu.cn/IMIsd/gjjr/f6b95a496429412e84990d93a6b66cc4.htm)：Chinese Marxist economics uses 世界货币 for Marx’s world-money function.（search_snippet；Search snippet; page body not independently inspected.）

### 110. Weltgeschichte → 世界历史

ID：`weltgeschichte`；状态：**限语境或变体需处理**；建议译法：世界历史。

Chinese scholarship supports noun 世界历史. This does not establish adjective weltgeschichtlich as interchangeable.

现有变体：weltgeschichtlich。

变体意见：weltgeschichtlich为世界历史的或世界历史性的，不能机械套用名词。

实际查询：

- `Weltgeschichte weltgeschichtlich 世界历史 世界历史性 德意志意识形态 德文 中文`
- `weltgeschichtlich 世界历史性`

来源及支持范围：

- [《德意志意识形态》（中文文本）](https://www.marxists.org/chinese/pdf/marx-engels/mes01.pdf)：Chinese edition uses 世界历史 for the noun; this does not establish equivalence with adjective weltgeschichtlich.（contextual；Search snippet; page body not independently inspected.）

### 111. Wertausdruck → 价值表现

ID：`wertausdruck`；状态：**已确认**；建议译法：价值表现。

Chinese Capital passage supports 价值表现.

实际查询：

- `Wertausdruck 价值表现 Marx 德文 资本论 中文`

来源及支持范围：

- [《资本论》第一卷第一章：商品](https://www.diancangwang.cn/waiguomingzhu/ac82405c9cbe/909b5d49b4e0.html)：Chinese text labels the form of value expression as 价值表现 and includes the German headword Wertausdruck.（same_section_cross_language；Search result snippet shows Wertausdruck with the Chinese passage.）

### 112. Wertform → 价值形式

ID：`wertform`；状态：**已确认**；建议译法：价值形式。

Chinese Capital text and scholarship support 价值形式.

实际查询：

- `Wertform 价值形式 Marx 德文 资本论 中文`

来源及支持范围：

- [《资本论》第一卷第一章：商品](https://www.marxists.org/chinese/marx/capital/01.htm)：Chinese Capital text and scholarship support 价值形式.（search_snippet；Search result snippet; page body not independently inspected.）

### 113. Wertformanalyse → 价值形式分析

ID：`wertformanalyse`；状态：**限语境或变体需处理**；建议译法：价值形式分析。

Chinese scholarship uses 价值形式分析 for the named interpretive method.

实际查询：

- `Wertformanalyse 价值形式分析 Marx 德文 中文`

来源及支持范围：

- [《浙江社会科学》：价值形式分析的方法](https://zjsh.cbpt.cnki.net/portal/journal/portal/client/paper/358e1c8c25b11b3cada10883f5d1c676)：Chinese scholarship uses 价值形式分析 as a named method.（contextual；Search snippet; page body not independently inspected.）

### 114. Wertgegenständlichkeit → 价值对象性

ID：`wertgegenstandlichkeit`；状态：**限语境或变体需处理**；建议译法：价值对象性。

Chinese Capital text explicitly renders Wertgegenständlichkeit as 价值对象性.

实际查询：

- `Wertgegenständlichkeit 价值对象性 Marx 德文 中文`

来源及支持范围：

- [周嘉昕：“物象化”、“物化”还是“对象化”？](https://ptextold.nju.edu.cn/ce/52/c12201a249426/page.htm)：Article prints Wertgegenständlichkeit with 价值对象性 in a discussion of Marx’s Capital text.（search_snippet；Search snippet; page body not independently inspected.）

### 115. Wertgröße → 价值量

ID：`wertgroe`；状态：**已确认**；建议译法：价值量。

Chinese translation supports 价值量.

实际查询：

- `Wertgröße 价值量 Marx 德文 中文`

来源及支持范围：

- [《资本论》第一卷第一章：商品](https://www.marxists.org/chinese/marx/capital/01.htm)：Chinese translation supports 价值量.（search_snippet；Search result snippet; page body not independently inspected.）

### 116. Wertmaß → 价值尺度

ID：`wertma`；状态：**已确认**；建议译法：价值尺度。

Chapter 3 Chinese text uses 价值尺度; the paired German section title is Maß der Werte.

现有变体：Maß der Werte。

变体意见：{'Maß der Werte': 'Search evidence does not establish unconditional interchangeability; preserve contextual distinction.'}

实际查询：

- `Wertmaß Maß der Werte 价值尺度 Marx 德文 中文`

来源及支持范围：

- [《资本论》第一卷第三章：货币或商品流通](https://www.marxists.org/chinese/marx/capital/03.htm)：Chapter gives 价值尺度; German section heading Maß der Werte is paired with this Chinese section in the bilingual contents.（same_section_cross_language；Search result snippets show Chinese chapter text and bilingual contents.）

### 117. Wertsubstanz → 价值实体

ID：`wertsubstanz`；状态：**已确认**；建议译法：价值实体。

The Chinese section heading explicitly pairs 价值实体 with Wertsubstanz.

现有变体：Substanz des Werts。

变体意见：{'Substanz des Werts': 'Search evidence does not establish unconditional interchangeability; preserve contextual distinction.'}

实际查询：

- `Wertsubstanz Substanz des Werts 价值实体 Marx 德文 中文`

来源及支持范围：

- [《资本论》第一卷第一章：商品的两个因素](https://marx-ebooks.github.io/docs/MEW-ZH/23/MEW23-008.html)：Chinese heading explicitly gives 价值实体 with German Wertsubstanz in parentheses.（same_section_cross_language；Search result snippet displays German term and Chinese rendering.）

### 118. Werttheorie → 价值论

ID：`werttheorie`；状态：**限语境或变体需处理**；建议译法：价值论。

马克思价值研究中价值论和价值理论均有学术用例；NJU文章在monetäre Werttheorie完整搭配中直接使用货币价值论，支持本条译法，但不能据此认定所有学科只有价值论这一译名。

实际查询：

- `Werttheorie 价值论 Marx 德文 中文`
- `"Werttheorie" "价值论" 马克思`

来源及支持范围：

- [李乾坤：马克思价值形式理论研究的历史演进与当代展望](https://ptext.nju.edu.cn/info/1348/9207.htm)：论文使用价值理论、价值论，并直接对照货币价值论与monetäre Werttheorie；此证据对独立词项属于搭配和语境支持。（学术研究；搜索结果摘要；未逐页全文核读）

### 119. Wertzusammensetzung → 价值构成

ID：`wertzusammensetzung`；状态：**限语境或变体需处理**；建议译法：价值构成。

Chapter 23 explains value composition through the constant/variable capital ratio; this supports a contextual rendering, not a fixed dictionary equivalent.

实际查询：

- `Wertzusammensetzung 价值构成 不变资本 可变资本 Marx 德文 中文`

来源及支持范围：

- [《资本论》第一卷第二十三章：资本主义积累的一般规律](https://www.marxists.org/chinese/marx/capital/23.htm)：Chinese text explains capital value-composition through the constant/variable capital ratio; German Chapter 23 uses Wertzusammensetzung. Contextual support only.（same_chapter_cross_language；Search results cross-identify Chinese chapter and German original wording.）

### 120. Wesensphilosophie → 本质哲学

ID：`wesensphilosophie`；状态：**限语境或变体需处理**；建议译法：本质哲学。

A Chinese translation excerpt directly pairs 本质哲学 with Wesensphilosophie; source provenance and broader standardization remain limited.

实际查询：

- `Wesensphilosophie 本质哲学 Marx 德文 中文`
- `Wesensphilosophie 中文哲学术语 德文`

来源及支持范围：

- [阿多诺：《本真性的黑话：论德意志意识形态》中译摘录](https://www.scribd.com/document/932136607/本真性的黑话-论德意志意识形态-阿多诺)：Search snippet directly pairs 本质哲学 with Wesensphilosophie in the Chinese translation excerpt; supports the target but source is an uploaded excerpt, not an authoritative full edition.（published_translation_excerpt；Search result snippet only; Scribd upload provenance and page body not independently verified.）

### 121. Zahlungsmittel → 支付手段

ID：`zahlungsmittel`；状态：**已确认**；建议译法：支付手段。

The bilingual section heading plus a German–Chinese dictionary support 支付手段.

实际查询：

- `Zahlungsmittel 支付手段 Marx 德文 中文`

来源及支持范围：

- [Langenscheidt 德汉词典：Zahlungsmittel](https://de.langenscheidt.com/deutsch-chinesisch/zahlungsmittel)：Dictionary gives 支付手段.（dictionary_entry；Search snippet; page body not independently inspected.）

### 122. zinstragendes Kapital → 生息资本

ID：`zinstragendes-kapital`；状态：**已确认**；建议译法：生息资本。

Chinese Capital Volume III supports 生息资本.

实际查询：

- `zinstragendes Kapital 生息资本 Marx 德文 中文`

来源及支持范围：

- [《资本论》第三卷第二十一章：生息资本](https://www.marxists.org/chinese/marx-engels/25/022.htm)：Chinese chapter heading and text use 生息资本.（search_snippet；Search snippet; page body not independently inspected.）
- [Das zinstragende Kapital](https://biancahoegel.de/archiv/marx_engels/kapital/bd3/me25_607.htm)：German text labels the concept zinstragendes Kapital.（search_snippet；Search snippet; page body not independently inspected.）

### 123. Zirkulationsmittel → 流通手段

ID：`zirkulationsmittel`；状态：**已确认**；建议译法：流通手段。

German and Chinese Capital text headings pair Zirkulationsmittel with 流通手段.

实际查询：

- `Zirkulationsmittel 流通手段 Marx 德文 中文`

来源及支持范围：

- [《资本论》第一卷第一篇：商品与货币](https://www.diancangwang.cn/waiguomingzhu/ac82405c9cbe/909b5d49b4e0.html)：Chinese contents pair 流通手段 with Zirkulationsmittel.（same_section_cross_language；Search snippet displays bilingual section heading.）

### 124. Äquivalentform → 等价形式

ID：`aquivalentform`；状态：**已确认**；建议译法：等价形式。

Chinese Capital text supports 等价形式.

实际查询：

- `Äquivalentform 等价形式 Marx German Chinese 资本论`

来源及支持范围：

- [《资本论》第一卷第一章：商品](https://www.marxists.org/chinese/marx/capital/01.htm)：Chinese section heading uses 等价形式 in the corresponding German Äquivalentform discussion.（same_section_cross_language；Search result snippet from the bilingual section.）

### 125. Überbau → 上层建筑

ID：`uberbau`；状态：**限语境或变体需处理**；建议译法：上层建筑。

Chinese Marx text supports 上层建筑; ideologischer Überbau is a narrower descriptive compound.

现有变体：ideologischer Überbau。

变体意见：ideologischer Überbau含思想或意识形态限定，不能总是省略。

实际查询：

- `Überbau ideologischer Überbau 上层建筑 Marx 德文 中文`

来源及支持范围：

- [Prawer：《实践和意识形态》](https://www.marxists.org/chinese/reference-books/prawer-1976/05.htm)：Chinese text quotes Überbau and renders it as 思想上层建筑 in the German Ideology discussion.（search_snippet；Search snippet; page body not independently inspected.）

### 126. Überproduktionskrise → 生产过剩危机

ID：`uberproduktionskrise`；状态：**已确认**；建议译法：生产过剩危机。

PONS德汉词典直接列生产过剩危机，当前对应有明确词典证据。

实际查询：

- `Überproduktionskrise 生产过剩危机 Marx 德文 中文`
- `"Überproduktionskrise" "生产过剩危机"`

来源及支持范围：

- [PONS德汉词典：Überproduktionskrise](https://de.pons.com/%C3%BCbersetzung/deutsch-chinesisch/%C3%9Cberproduktionskrise)：直接释为生产过剩危机。（出版机构双语词典；搜索结果摘要；未逐页全文核读）


## 落实记录（2026-10-28）

- 历史审计保留126条；当前词表删除6项：`dialektische-darstellung`、`geld-als-selbstzweck`、`produktionskostentheorie`、`scheinausgleich`、`subjektive-arbeitswertlehre`、`theoretisches-feld`。当前有效条目数为120，未新增条目。
- 采用6项建议译名：`charaktermaske`（性格面具）、`erscheinungsform`（表现形式）、`formelle-subsumtion`（形式从属）、`kapital-im-allgemeinen`（资本一般）、`reelle-subsumtion`（实际从属）、`transformationsproblem`（转形问题）。`verdinglichung`保留“物化”。
- 清理19组中的22个明确不等义变体字符串；逐项映射见配套JSON的 `application.variant_removals`。
- 对市民社会/资产阶级社会、Geltung、Gattungswesen、Privateigentum及Fetisch词组补入简短中文语境说明。
- 历史基线SHA-256：`cc3d62353df8623fc32b34eca5cfa52f819c9b73d420b0919ee8dcb409382a48`。更新后SHA-256：`309e80d460edebb6484666c010b9d2150a5d00aa281578dfc35c4b5a44432e65`。
