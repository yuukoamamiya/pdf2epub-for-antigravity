# 外部领域术语表

这些只读术语表仅在书籍配置中明确选择后加载。可运行
`uv run pdf2epub -c config.yaml glossary-candidates` 查看候选；程序不会自动选择领域。
选中的原生 schema 2 文件会生成审计快照，翻译 worker 只收到按源单元和当前语言对裁剪的稀疏上下文。

## Schema 2

每张表包含 `metadata.id`、`metadata.name`、`metadata.domain` 和 `metadata.version`。
每个概念有稳定唯一的 `id`，并在 `terms` 下按语言代码列出词形。每种语言至少有
`preferred`，可选 `variants`、`aliases`、`policy` 和 `allow_short`。
目前常见语言代码包括 `en`、`de`、`fr` 和 `zh-Hans`。

```yaml
schema_version: 2
metadata:
  id: german-classical-philosophy
  name: German Classical Philosophy
  domain: german classical philosophy
  version: "2026-11-03"
entries:
  - id: german-classical-philosophy:aufheben
    terms:
      en:
        preferred: sublation
        aliases: [sublate]
        policy: fixed
      de:
        preferred: aufheben
        policy: fixed
      zh-Hans:
        preferred: 扬弃
        policy: fixed
    provenance:
      source_table: german-classical-philosophy.yaml
      source_id: aufheben
```

每个配置的源/目标语言对直接从 `terms` 取词；缺少某一语言时不从其他语言推断。
`policy` 设在语言词条上；投影时采用目标语言词条的策略（`fixed` 或 `preferred`）。
需要跨语言参考时，在对应源语言词条的 `aliases` 中明确记录可匹配的源文形式。
合并时无法可靠分类的旧词形留在 `legacy_unclassified_forms`/`provenance` 中，仅供审计，
不作为活动匹配词形。不同原表的概念保持独立 ID，即使译文相同也不自动合并。

## 合并表与原路径

| 当前表 | 原表 |
|---|---|
| `marxism.yaml` | `marxist-political-economy-en-zh.yaml`、`marxist-theory-de-zh.yaml` |
| `german-classical-philosophy.yaml` | `german-classical-philosophy.yaml`（未改名） |
| `psychoanalysis.yaml` | `lacanian-psychoanalysis-en-zh.yaml` |
| `communication.yaml` | `political-economy-of-communication.yaml` |
| `games.yaml` | `crpg-common-zh-cn.yaml`、`nintendo-common-zh-cn.yaml` |

请把本地配置中的旧路径更新为当前表名；软件不提供旧路径兼容或自动迁移。
游戏词表保留 CRPG 与任天堂游戏等具体子域，避免跨类型泛化。
