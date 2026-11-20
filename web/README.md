# OCR demo page + optional proxy

`index.html` 是单页 OCR 演示：浏览器直连 OCR 端点，遇到 CORS 时回退到
`/api/ocr`（Cloudflare Pages Function），由它转发 `{ base_url, api_key, payload }`。

本地运行：

```bash
cd web && npx wrangler pages dev .
```

环境变量（本地写 `.dev.vars`，线上用 Pages secrets）：

- `ALLOWED_OCR_HOSTS`：`base_url` 的 `host[:port]` 白名单，逗号分隔。
  默认 `127.0.0.1:11434,localhost:11434`，即只允许本机 Ollama。
- `ALLOWED_ORIGINS`：允许调用代理的来源，逗号分隔；`ALLOWED_ORIGIN` 优先并固定单值。

请求体上限 10 MB，`base_url` 只接受 `http:`/`https:`。该代理是可选的本地演示
中继，生产部署必须显式设置上述白名单。
