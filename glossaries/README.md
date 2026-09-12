# 外部领域术语表

外部领域术语表不会全局生效，只有在某本书的配置中写入
`translation.glossaries` 后才会被加载。支持 `.yaml`、`.yml` 和 `.json`。

最小格式：

```yaml
schema_version: 1
metadata:
  name: german-classical-philosophy
  domain: German classical philosophy
  source_language: German
  target_language: Chinese
  version: "2026-09"

entries:
  - id: aufhebung
    source: Aufhebung
    variants: [aufheben, aufgehoben]
    target: 扬弃
    policy: fixed
    note: 黑格尔辩证法术语
```

`policy` 可以是 `fixed` 或 `preferred`。同一个源词及其变体不能在所选术语表
中对应不同的译法。术语表原文件只读，程序会在书籍输出目录保存规范化快照并锁定
SHA-256。
