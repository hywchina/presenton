# Offline Qwen3-VL file-generation API

Presenton exposes one multipart API that accepts text plus up to eight images and
returns one generated file. Runtime inference uses the configured local
OpenAI-compatible Qwen3-VL endpoint only. Markdown and DOCX are rendered locally;
PPTX reuses Presenton's local templates and export runtime.

## API

`POST /api/v1/generate-file`

Multipart form fields:

| Field | Required | Description |
| --- | --- | --- |
| `text` | if no image | Generation request/source text |
| `images` | if no text | Repeatable image file field, maximum 8 |
| `type` | yes | `word`, `md`, or `ppt` |
| `filename` | no | Download filename without or with extension |
| `language` | no | Output language; defaults to `Chinese` |
| `n_slides` | for PPT only | 1-20; defaults to 6 |
| `template` | for PPT only | Local Presenton template; defaults to `general` |

The response body is the generated `.docx`, `.md`, or `.pptx` file. The response
also includes `X-Generated-File-Type`; PPT responses include `X-Presentation-ID`.

### DOCX

```bash
curl --noproxy '*' -X POST http://127.0.0.1:5001/api/v1/generate-file \
  -F 'text=根据图片生成一份产品分析报告' \
  -F 'images=@./product.png' \
  -F 'type=word' \
  -F 'filename=产品分析报告' \
  -o report.docx
```

### Markdown

Markdown embeds uploaded images as data URIs so the result remains one offline
file.

```bash
curl --noproxy '*' -X POST http://127.0.0.1:5001/api/v1/generate-file \
  -F 'text=整理为技术说明' \
  -F 'images=@./diagram.png' \
  -F 'type=md' \
  -o result.md
```

### PowerPoint

```bash
curl --noproxy '*' -X POST http://127.0.0.1:5001/api/v1/generate-file \
  -F 'text=生成一份六页项目汇报' \
  -F 'images=@./chart.png' \
  -F 'type=ppt' \
  -F 'n_slides=6' \
  -F 'template=general' \
  -o result.pptx
```

## Local development configuration

The recommended local entrypoint is the management script in the repository
root. It starts both FastAPI and the Next.js renderer required by PPT export:

```bash
./presenton.sh start
./presenton.sh status
./presenton.sh logs -f
./presenton.sh stop
```

The Qwen3-VL process is external to Presenton. The script checks it through
`/v1/models`, but does not start or stop it. Run `./presenton.sh help` for port,
model, component, and production-mode overrides.

For manual startup, run Presenton directly on the same host as Qwen3-VL with:

```bash
export LLM=custom
export CUSTOM_LLM_URL=http://127.0.0.1:18081/v1
export CUSTOM_LLM_API_KEY=rail-vllm-test-key
export CUSTOM_MODEL=qwen3-vl-8b-instruct
export LLM_MAX_OUTPUT_TOKENS=2048
export ICON_SEARCH_MODE=lexical
export DISABLE_IMAGE_GENERATION=true
export WEB_GROUNDING=false
export MEM0_ENABLED=false
export DISABLE_ANONYMOUS_TRACKING=true
```

The service may call the same Qwen3-VL model several times for one PPT request:
once for multimodal planning, then for template/slide content. It never requires a
second model.

## Docker runtime

When Qwen3-VL runs on the Docker host, `127.0.0.1` inside Presenton's container
would point back to Presenton. The offline Compose override maps the host gateway
and uses `host.docker.internal`:

```bash
cp config/offline.env.example config/offline.env
# Edit QWEN3_VL_MODEL to the exact /v1/models id.
docker compose \
  --env-file config/offline.env \
  -f docker-compose.yml \
  -f docker-compose.offline.yml \
  up --build production
```

The Docker image build itself downloads and bundles Python, Node, Chromium,
fonts, OCR data, and the presentation-export runtime. Build it on a connected
machine once, then transfer the completed image to the offline target. Runtime
generation is offline.

## Runtime constraints

- Images are validated, normalized to PNG, and resized to at most 2048 pixels on
  either edge before inference.
- Each image is limited to 20 MB; the total request image payload is limited to
  64 MB.
- Web search and external image generation must remain disabled.
- The offline profile uses deterministic lexical matching over bundled SVG icons;
  it does not load the optional FastEmbed icon model.
- Use only bundled templates/fonts for guaranteed offline PPTX output.
- The configured vLLM server must accept multimodal Chat Completions and JSON
  Schema structured output.
- The supplied model advertises an 8192-token context window, so the offline
  profile caps output at 2048 tokens and preserves room for template schemas,
  prompts, and images.
