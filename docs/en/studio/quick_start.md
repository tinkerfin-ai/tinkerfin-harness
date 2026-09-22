# Try Studio

[Quick Start](../quick_start.md) · [中文](../../cn/studio/quick_start.md)

## Prerequisites

- Docker, Docker Compose 2.24+, and Bash; use WSL on Windows
- Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Node.js 20.19+ (20.x) or 22.12+, and pnpm 10.8.0
- A model provider endpoint, model name, and API key

Docker runs MySQL, Redis, OpenSandbox, and MinIO. Run the Studio backend and Web client on your machine.

## Start the backend

Prepare dependencies and local configuration with one command, then start the Python backend:

```bash
git clone https://github.com/tinkerfin-ai/tinkerfin-harness.git
cd tinkerfin-harness
./apps/studio/server/deploy/start.sh --local
uv sync --package tinkerfin-studio --locked
uv run --package tinkerfin-studio python -m tinkerfin_studio
```

`--local` waits for the four services, creates `apps/studio/.env` with random passwords from the single `apps/studio/.env.example` template, and selects the `tinkerfin` bucket on first setup. Repeated runs reuse configuration and data. Adjust `apps/studio/.env` first if a port is occupied.

In PyCharm, select the repository's `.venv`, set the working directory to the repository root, and run the `tinkerfin_studio` module.
After startup: [readiness](http://127.0.0.1:8090/health/ready) · [API docs (Swagger)](http://127.0.0.1:8090/docs). Workspaces are created on demand without a warm pool; each new sandbox is limited to 1 CPU and 1 GiB of memory. The runtime image is downloaded on first use if needed.

To run the backend and its dependencies in containers, use the following command with the same `.env`. Python and uv are not required on the host:

```bash
./apps/studio/server/deploy/start.sh --container
```

To choose your own passwords, change deployment addresses, or connect to existing services, first run `./apps/studio/server/deploy/init-env.sh`, then edit `.env` before starting. `COMPOSE_PROFILES` selects the bundled services. Run `start.sh` without a mode flag to use an available published image. See [server deployment](../../../apps/studio/server/README.md) for remote access and file log mount requirements.

## Start the Web client

Open another terminal at the repository root:

```bash
cd apps/studio/web
corepack enable
corepack prepare pnpm@10.8.0 --activate
pnpm install --frozen-lockfile
pnpm dev
```

Open the address printed in the terminal, normally `http://localhost:5190`. The login page accepts a server address; leaving it blank connects to `http://127.0.0.1:8090`. The browser saves the address automatically. The backend allows any frontend origin. An HTTPS page requires an HTTPS server.

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

Change the initial password before exposing the service. Account updates are administrator operations. Generate a new password hash from the repository root:

```bash
uv run --package tinkerfin-studio python -c 'import asyncio, getpass; from tinkerfin_studio.auth.passwords import hash_password; print(asyncio.run(hash_password(getpass.getpass("New password: "))))'
```

If the backend runs in a container, use this command from the repository root instead:

```bash
docker compose --env-file apps/studio/.env -f apps/studio/server/deploy/docker-compose.yaml exec server python -c 'import asyncio, getpass; from tinkerfin_studio.auth.passwords import hash_password; print(asyncio.run(hash_password(getpass.getpass("New password: "))))'
```

Connect to the `tinkerfin` business database with your database client and use the full printed hash:

```sql
UPDATE users SET password_hash = '<full generated hash>' WHERE username = 'tinkerfin';
```

Conversations default to Full access. Use the permission picker beside the model
to require write approval. For scheduled work, see [Studio automation](automation.md).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The page cannot reach the server | Backend readiness and the server address on the login page |
| Sign-in fails | Whether the account exists in this data volume and whether its password was changed |
| Sending is disabled | An enabled default model and completed attachment uploads |
| Conversation error | Backend logs, model configuration, and OpenSandbox availability |

[Documentation](../index.md) · [Web development](../../../apps/studio/web/README.md)
