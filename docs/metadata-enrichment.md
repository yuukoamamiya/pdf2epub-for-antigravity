# 元数据识别与补全

本流程为 PDF→EPUB、EPUB/HTML 翻译和轻小说 EPUB 重建提供可追溯的书目元数据证据。TeX/arXiv 翻译流程当前不支持 metadata enrichment；`required` 不会为 TeX 增加元数据阶段，也不能用于要求 TeX 阶段通过元数据门禁。它只补充可核验的出版信息，不判断或改写正文，不替代 OCR、结构、正文翻译和正文校验门禁。语义判断由 Antigravity 工作区 Subagent 按本地生成的 scoped handoff 完成；Codex 不得自行判定作品身份、出版物属性或候选书目是否匹配。

## 配置

```yaml
metadata_enrichment:
  mode: auto                 # auto | disabled | required
  online: true
  fields: [description, subjects, identifiers, publishing]
  # 若 required_fields 要求 cover，也必须把 cover 加进 fields
  # fields: [description, subjects, identifiers, publishing, cover]
  cover_policy: preserve     # preserve | fill_missing | prefer_verified
  evidence_pages: 5          # 本地 PDF 页证据数，自动限制为 1–20
  # required_fields: [identifiers]
```

`auto` 是默认模式：尽力核验并使用部分成功的字段；符合适用条件时，未找到资料或查询失败不会中断现有信息和后续流程。`required` 才会因适用性或所需元数据不完整而阻断。若省略 `required_fields`，当前请求的 `fields` 都作为强制项；显式提供非空 `required_fields` 时，仅该子集强制。在 `required` 模式显式配置 `required_fields: []` 是配置错误，不能用空列表关闭字段门禁。经审阅确认 `non_publication` 的输入可记为 `not_applicable`；`uncertain` 在 auto 下跳过元数据补全并继续原有流程，在 required 下阻断直到获得可验证决定。`disabled` 完全跳过本功能。`fields` 控制请求复核的补全字段；不应把它理解为要求每项都成功。

`evidence_pages` 默认 5，控制 evidence 中最多纳入的 PDF 页面数；配置值会限制在 1–20。`required_fields` 可包含 `cover`，在 `required` 模式中要求封面字段达到可接受的终态；低于建议尺寸的封面只有在决定中显式标记 `accept_below_target: true` 后才可按 `improved_below_target` 通过。

`cover_policy` 控制候选封面如何参与处理：`preserve` 保留原有行为，不插入或替换候选封面；`fill_missing` 仅在成品缺少封面时补入已复核封面；`prefer_verified` 可优先采用已复核候选。封面候选须经 Antigravity 工作区 Subagent 查看实际图像并核对来源和版本关系，必要时交人类判断，之后才能进入构建。

## 操作流程

新生产在 `auto` 或 `required` 模式下都必须完成以下阶段；只有 `disabled` 才省略。旧产物也可按同样命令单独恢复元数据补全。首先准备本地证据：

```text
uv run pdf2epub -c <配置> metadata-prepare
# 恢复时仅复用未变化的证据和 handoff
uv run pdf2epub -c <配置> metadata-prepare --resume
```

准备阶段只收集有限的本地证据并生成任务合同，不调用模型或网络。PDF 页面证据由 `evidence_pages` 限定（默认 5，最大 20）。`--resume` 仅在输入证据和 handoff 上下文未变化时复用已有材料；否则会刷新合同。读取 `output/<title>/metadata_enrichment/metadata_subagent_prompt.md`、`manifest.json` 和 pending 字段，然后把 handoff 交给 Antigravity 工作区 Subagent。metadata worker 占用 `subagent.batching.max_concurrency` 全局并发额度，不与 extreme wave 重叠，也无权调度其他 worker。Subagent 只读取合同列出的证据，并把决定写入 `decision.json`。不要在聊天里代写决定，也不要让 Subagent 扫描全书正文。

决策至少要分别说明作品身份与适用性，并为每个已核验字段引用 `evidence.json` 中精确的 `location` 和 SHA-256。身份不能确认、私人文稿/笔记或混合集合无法判定时，应标为 `uncertain`；auto 下跳过补全继续流程，required 下等待工作区 Subagent 复核或人类判断；有证据支持非出版物时标为 `non_publication`。不能因为缺少 ISBN 或文件名看起来不像书而判为非出版物。正式图书的扫描件或重排版可以匹配其底本，但必须写清输入制品与底本版本的关系。论文可使用 DOI、ISSN 等标识，不要求 ISBN。身份记录中的 `source_language` 使用语言代码（2–3 位小写语言码，可带 `-` 加两位大写地区码，例如 `en`、`ja`、`pt-BR`）；展示给人的自然语言名称不作为该字段值。`identity.source_edition`、`identity.artifact_relation` 和 `identity.source_language` 会作为已核验 `source_context` 随元数据计划进入构建，用于表达源版本及译制关系。

字段规则：

- 区分底本出版物书目信息和本次译制文件。底本 ISBN 不是译制文件的 ISBN；若记录底本 ISBN，须说明其角色、源语言和版本关系。
- 保留 DOI、ISSN 及其原始关系，不用 ISBN 替代其他标识。
- 出版日期表示出版记录日期，不表示本地转换、翻译或生成 EPUB 的时间。
- 保留作者、编者、译者等角色及顺序，不把译者误记为原作者。
- 简介、主题词和封面等语义字段由 Antigravity 工作区 Subagent 基于引用证据复核；网络搜索候选只作为线索，不能自动变成已核验数据。
- 用户提供的私人书目说明可作为有出处的 source note 证据引用，不能把它伪装成公共数据库记录。
- `publishing` 的 value 是对象，可包含 `publisher` 文本和 `date`（`YYYY`、`YYYY-MM` 或 `YYYY-MM-DD`）。`identifiers` 是对象；ISBN 要通过校验位校验并带有 `source_print`、`official_ebook`、`translation_source` 或 `corresponding_print` 角色。DOI/ISSN 会检查格式，底本 ISBN 角色使用 `translation_source`（也可依实际证据使用 `source_print` 或 `corresponding_print`）；角色为 `translation_source` 时，必须填写语言代码格式的 `source_language` 和非空 `relationship_note`，并且该语言必须与配置或已复核的作品身份源语言一致。
- 简介必须是单个 `<p>...</p>` 段；目标语言为中文时纯文本长度须为 150–300 字符。中文 `subjects` 要有 3–5 个互异标签。

Subagent 将适用性确认为 `publication` 后，auto/required 生产继续运行有限在线检索：

```text
uv run pdf2epub -c <配置> metadata-fetch
```

检索只在当前证据合同有效且已确认 publication 与 identity 后执行。若适用性为 `non_publication`，记录为 `not_applicable` 并完成校验；若为 `uncertain`，auto 记录原因后跳过补全继续原流程，required 则阻断等待复核。网络不可用、证据不足或复核失败时，auto 记录原因、保留已有 metadata 并继续；不能把未尝试的阶段报告为已尝试失败。Google Books 和 Open Library 返回值是候选，不会自动写入有效 metadata。每条候选附有确定性 `identity_match` 提示（`title_overlap`、`author_overlap`、`hard_conflicts`、`summary`）；它只是标题/作者/出版者冲突线索，`matched: false` 仍保持 false，不是匹配或核验结论。若字段引用含 `hard_conflicts` 的候选，决定必须在该字段记录中给出非空 `match_review.summary` 与 `match_review.edition_relation`，说明实质性匹配理由及版本关系；校验器否则拒绝该字段。引用可用 `evidence_refs.location` 写 `fetch.json#<candidate_sha256>`，并提供相同候选哈希，或按生成 handoff 规定引用 `source_url` 与候选哈希。候选哈希包含 identity_match；未被决定引用的候选变化不会使已接受字段失效。网络失败或无候选时保留失败/未找到状态。网络连接使用标准 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量；需要代理时由操作员在当前 shell 按网络环境设置，例如 `HTTPS_PROXY=http://<proxy-host>:<proxy-port>`（Windows PowerShell 用 `$env:HTTPS_PROXY="http://<proxy-host>:<proxy-port>"`），不要把代理地址或凭据写入书籍配置。公共来源可能返回 HTTP 429；程序不对 429 做立即循环重试。默认可复用相同查询缓存；仅在需要重试失败来源时使用 `metadata-fetch --retry`。该开关只重试失败的来源；已有成功来源继续复用。新的候选必须回到 Subagent 复核，并把来源与候选哈希作为证据加入决定，不可直接把 `fetch.json` 当作批准结果。候选证据使用 `source_url` 与 candidate SHA-256 独立绑定，不改变本地 `evidence_sha256` 或使身份决定过期。

需要检查目录候选封面时，把候选 URL 明确传给下载命令：

```text
uv run pdf2epub -c <配置> metadata-fetch --cover-url <公开图片URL>
```

下载器仅接受公开 HTTP(S) 目标，限制响应大小和图像像素数，保存原始字节与按 EXIF 方向归一化的 RGB JPEG。`cover_candidate.json` 记录来源、两份哈希和尺寸；下载不代表核验。只有 Antigravity 工作区 Subagent 基于实际图像完成目视复核，并在决定中引用当前 JPEG 哈希、说明封面与本次底本的版本关系后，`metadata-validate` 才会接受 cover 字段。决定须填写 `kind`（`cover`、`journal_cover` 或 `article_page`）、非空字符串 `visual_review` 和 `version_relation`。默认目标尺寸为短边至少 650、长边至少 1000，长短边比例不得超过 3；低于目标的图片须明确设置 `accept_below_target: true`，并在校验报告中记录 `improved_below_target`。状态输出中的 `cover.quality` 为 `recommended`（短边至少 650、长边至少 1000 且比例不超过 3）、`below_target`（未达到建议边长）或 `placeholder_shape`（长短边比例超过 3）；未下载候选时为 `null`。`required` 的严格门禁由 `required_fields` 决定：若把 `cover` 列为必需项，低分辨率需显式接受并通过视觉复核，畸形比例仍不能作为有效封面。

`cover_policy: preserve` 是默认值，保留源封面策略；`fill_missing` 只在缺失时补入，`prefer_verified` 允许使用已核验候选优先替换。

Subagent 写完 `decision.json` 后运行：

```text
uv run pdf2epub -c <配置> metadata-validate
uv run pdf2epub -c <配置> metadata-status --json
```

只有当前源、证据、决定哈希和字段证据均通过验证，字段才可用于构建。报告中的 stale 状态应先重新准备并复核，不得沿用旧决定。`metadata-status --json` 纯只读地重算当前性并汇报阶段状态，不写验证收据，也不替代正式校验。

## 最小决定示例

以下仅展示合同形状，不可复制为生产决定；实际值、字段类型必须以本次 Subagent prompt 和 `evidence.json` 为准。

```json
{
  "schema_version": 1,
  "evidence_sha256": "<manifest 中的当前 evidence_sha256>",
  "applicability": {
    "status": "publication",
    "rationale": "版权页明确给出该版出版信息",
    "evidence_refs": [{"location": "pages/0001.md", "sha256": "<该证据项哈希>"}]
  },
  "identity": {
    "status": "verified",
    "document_type": "published book",
    "work_title": "Example Work",
    "authors": [{"name": "Example Author", "role": "author"}],
    "source_edition": "English source edition",
    "artifact_relation": "scanned copy of the cited source edition",
    "source_language": "en",
    "evidence_refs": [{"location": "pages/0001.md", "sha256": "<该证据项哈希>"}]
  },
  "fields": {
    "identifiers": {
      "status": "verified",
      "value": {"doi": "10.1234/example.2024"},
      "evidence_refs": [{"location": "pages/0001.md", "sha256": "<该证据项哈希>"}]
    }
  }
}
```

若记录底本 ISBN，`identifiers.value` 可按以下形状写入；该 ISBN 指向源版本，不代表译制 EPUB 获得了自己的 ISBN：

```json
{
  "status": "verified",
  "value": {
    "isbn": "9780306406157",
    "isbn_role": "translation_source",
    "source_language": "en",
    "relationship_note": "ISBN identifies the English print source edition used for this translation."
  },
  "evidence_refs": [{"location": "pages/0001.md", "sha256": "<该证据项哈希>"}]
}
```

可以只提交已核验的部分字段；未核验字段标记 `needs_review`、`not_found` 或 `not_applicable`，不得填入猜测值。标为 `verified` 的字段要求引用非空且完全匹配当前证据的引用。若字段采用发生身份冲突的目录候选，需在字段记录附上 `match_review`；示意：

```json
{
  "status": "verified",
  "value": {"title": "Example Work"},
  "evidence_refs": [{"location": "fetch.json#<candidate_sha256>", "sha256": "<candidate_sha256>"}],
  "match_review": {
    "summary": "作者、主要标题和版权页证据一致；目录标题差异来自副标题。",
    "edition_relation": "该候选是输入扫描件所对应的英文平装版。"
  }
}
```

## 产物与恢复

每次 `metadata-prepare` 成功后，核对 prompt、manifest 和 `pending_fields`，再进行 Subagent handoff。完成后执行 `metadata-validate` 建立校验收据，随后以 `metadata-status --json` 检查状态；status 只读。auto 下为 `uncertain`、`incomplete` 或查询失败时，打包保留现有可用值继续流程，并在最终运行状态中简短提示元数据补全未完成，不应报告为全部已核验。

所有专属产物位于 `output/<title>/metadata_enrichment/`：

- `evidence.json`：有界本地证据及来源哈希；
- `manifest.json`、`metadata_subagent_prompt.md`：任务合同和 scoped prompt；
- `decision.json`：Subagent 写入的适用性、身份和字段决定；
- `fetch.json`：可选在线候选、来源响应哈希、失败和查询缓存；
- `cover_candidate.json`、`cover_candidate.jpg` 与 `cover_candidate.original.jpg`：可选封面候选、规范化 JPEG 和保留的原图；
- `validation.json`：本地验证结果及当前性信息。

准备命令刷新证据后，旧决定如果不再绑定当前哈希便不能复用。保留旧文件作为诊断材料，按新 prompt 重新做 scoped 复核；不要手工改哈希或伪造校验结果。网络中断时可重试失败来源。候选部分成功时复核并使用已核验字段，其他字段保持原值；`auto` 继续工作流。`required` 阻断时先补足所声明字段或把有证据的缺失原因处理为合法状态，再重新校验。不得通过关闭正文、OCR 或 package 门禁来绕过 metadata 阻断。

PDF→EPUB、HTML/EPUB 和轻小说 EPUB 构建只消费已验证且当前的字段；TeX/arXiv 当前没有此接入。构建收据须绑定实际采用的元数据、证据和封面输入。 metadata 阶段不重新翻译正文，也不替代正文全量校验。Calibre 自动入库不属于当前功能。
