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

Open the user menu at the bottom left, select Models, and choose Add provider. Select a provider or custom service, enter its URL and authentication, and save the connection.

Select that connection and use Fetch models, or enter the provider's Model ID manually. Enable a chat model, make it the default, and send “Hello” to check the connection.

For Ollama, start the service and install a model first. Select the native Ollama API; no placeholder key is needed. Container deployments need an address reachable from the container. An administrator must add local, private-network, and HTTP origins to `MODEL_ALLOWED_ORIGINS`; see the [server guide](../../../apps/studio/server/README.md).

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
