# Try Studio

[Quick Start](../quick_start.md) · [中文](../../cn/studio/quick_start.md)

## Prerequisites

- Docker, Docker Compose 2.24+, and Bash; use WSL on Windows
- Node.js 20.19+ (20.x) or 22.12+, and pnpm 10.8.0
- A model provider endpoint, model name, and API key

Docker runs the backend, database, Redis, and OpenSandbox. Start the Web client separately.

## Start the backend

Build and run from the repository:

```bash
git clone https://github.com/tinkerfin-ai/tinkerfin-harness.git
cd tinkerfin-harness/apps/studio/server/deploy
./deploy.sh --build
```

The first run creates configuration and service credentials, builds the backend image, and waits for readiness. Workspaces are created on demand without a warm pool; each new sandbox is limited to 1 CPU and 1 GiB of memory. The runtime image is downloaded on first use if it is not already available.
After startup: [readiness](http://127.0.0.1:8090/health/ready) · [API docs (Swagger)](http://127.0.0.1:8090/docs).

Use `./deploy.sh` when a published image is available to you. See [server deployment](../../../apps/studio/server/README.md) for external databases, configuration, and log commands.

## Start the Web client

Open another terminal at the repository root:

```bash
cd apps/studio/web
corepack enable
corepack prepare pnpm@10.8.0 --activate
pnpm install --frozen-lockfile
pnpm dev
```

Open the address printed in the terminal, normally `http://localhost:5173`.

## Sign in and configure a model

Initializing a new database creates this account:

| Field | Value |
| --- | --- |
| Username | `tinkerfin` |
| Password | `123456` |

Existing data volumes do not rerun the initialization SQL or overwrite accounts. There is no public registration endpoint.

Open the user menu at the bottom left, select Models, and choose Add provider. Select a preset such as Qwen, DeepSeek, OpenAI, or Ollama, or configure a custom service. Save its URL and authentication first. Models under one connection share its key; you can create multiple connections for the same provider.

Select a connection and use Fetch models to add the models you need, or enter the provider’s Model ID manually. A failed listing does not prove that generation is unavailable: you can still add a model and test it. Enable a model and make it the default as needed. Chat and image generation have separate defaults. Keys are saved through the form for the current user’s connection and are never returned in settings responses.

Chat supports OpenAI Chat Completions compatible endpoints and the native Ollama API. Leave generation parameters blank to use model defaults. DeepSeek and Ollama support the reasoning toggle; reasoning effort for other compatible services must match the selected model. Set image input support according to the model’s actual capabilities; unknown capability blocks image input.

Ollama defaults to `http://localhost:11434` and requires no placeholder key. Start the service and install models before connecting. For container deployments, use an address reachable from the container. Local, private-network, and HTTP endpoints must be included in the administrator’s `MODEL_ALLOWED_ORIGINS`; see the [server guide](../../../apps/studio/server/README.md). Download and delete models through Ollama.
Capability tests in model settings call the selected provider and may incur usage charges; review your configuration before testing.
Send “Hello” to check the connection, then try attachments or plan mode. Image inputs require a model with image support; image generation requires a separately configured provider.

Attachments support images, Markdown, PDF, DOCX, and XLSX. Markdown files use UTF-8 with a `.md` or `.markdown` extension. Upload them for the agent to read by line, or ask it to generate and deliver a Markdown file. Click the filename to preview headings, tables, and code blocks, then download the original. Long previews show the first 100,000 characters; downloads retain the complete file.

DOCX previews show text and tables; download the original for images and layout. PDF uses your browser’s viewer. XLSX previews show saved values from the first sheet, up to 100 rows and 20 columns, without recalculating formulas. Files too large to preview remain downloadable.

## Change the initial password

Change the initial password before exposing the service. Account updates are administrator operations. Generate a new password hash from the backend's `deploy` directory:

```bash
docker compose exec server python -c 'import asyncio, getpass; from tinkerfin_studio.auth.passwords import hash_password; print(asyncio.run(hash_password(getpass.getpass("New password: "))))'
```

Connect to the Studio database with your database client and use the full printed hash:

```sql
UPDATE users SET password_hash = '<full generated hash>' WHERE username = 'tinkerfin';
```

Conversations default to Full access. Use the permission picker beside the model
to require write approval. For scheduled work, see [Studio automation](automation.md).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The page cannot reach the server | Backend readiness and the Web proxy address |
| Sign-in fails | Whether the account exists in this data volume and whether its password was changed |
| Sending is disabled | An enabled default model and completed attachment uploads |
| Conversation error | Backend logs, model configuration, and OpenSandbox availability |

[Documentation](../index.md) · [Web development](../../../apps/studio/web/README.md)
