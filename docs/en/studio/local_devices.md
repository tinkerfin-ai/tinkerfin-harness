# Run local tasks on a Mac

[Studio quickstart](quick_start.md) · [简体中文](../../cn/studio/local_devices.md)

The TinkerFin client lets Studio conversations use files, Python, Node, an isolated browser and MCP tools on your Mac. The Agent handles the conversation in the cloud while the client executes local tools; model provider keys stay in the cloud.

## Connect

1. Download the client from [GitHub Releases](https://github.com/tinkerfin-ai/tinkerfin-harness/releases), extract it and open it on an Apple Silicon Mac running macOS 15 or later. The app is not notarized by Apple; follow the [installation instructions](../../../apps/desktop/README.md#安装与连接) if macOS blocks it.
2. Click the menu bar icon, select its settings button to open the workbench, then enter your service address on **Connection**, then sign in to Studio in your browser and authorize the connection. After authorization succeeds, close the page and open the client from the menu bar.
3. Once the client shows **Connected**, select your Studio project and start a conversation. Ask for the local files or applications your task needs.

The client creates project workspaces automatically. No directory selection is required. **Computer resources** in the header shows the actual device name and connection status: green when connected, red when disconnected. Select the device row to open **Settings → Local devices**; closing settings returns to the same conversation.

Each account has one authorized device. A new authorization invalidates the previous pairing. A new turn binds the current device when it first needs computer tools. Pending approvals and resumed execution retain their original authorization and never move automatically to another computer. Under **My account**, use the edit button beside the device name, then press Enter or select the checkmark to save. **Sign out** revokes this device’s authorization and disconnects it; you can then authorize another account. Your browser’s Studio sign-in is unaffected.

Choose **System**, **简体中文** or **English** under **My account → Language**. Changes apply immediately and remain selected after restarting the client.

Under **My account**, view the current version and check for updates. After confirmation, the client downloads and verifies the package, waits for local operations to finish, installs it and restarts. You can cancel before installation begins. Device authorization and local preferences are preserved. Closing or minimizing the workbench hides the window; **Quit** in the menu bar ends the client.

## Projects and files

Local workspaces live under `~/.tinkerfin/projects/`, isolated by Studio service, account and project identity. The cloud retains project ownership. Renaming a project or reconnecting the same account does not change its local directory. Switching accounts preserves existing directories, which are reused when you return. Moving to another computer does not transfer files automatically.

One conversation can use both cloud and computer tools, with working files stored separately. Project memory is shared within the current account and project; view or edit it in **Memory management**. Using computer tools does not create a second memory collection. Tasks can import conversation attachments, and explicitly delivered results return as attachments for preview or download. Studio’s **Workspace** shows cloud files; open computer directories from the client.

The project directory is the task's default working directory. Specify an absolute native path or a `~/` path to work with files elsewhere; relative paths resolve from the project directory. No per-directory authorization is required. Client settings, task state, credentials, and managed directories for other accounts or projects remain protected, and system permissions still apply.

Conversations that have used computer files cannot be moved to another project. Start a new conversation in the target project when you need to work there.

## Permissions and recovery

**Computer resources** controls confirmation for computer tools in the current conversation. New conversations default to **Ask before changes**. Local file changes, every command (including browser commands), MCP calls, and desktop clicks or input use the existing approval card. Reading files, listing applications and viewing windows run directly. **Run automatically** skips these confirmations; both choices reach the same files. The selection is locked during execution or pending approval, and existing conversations retain their saved choice. Cloud sandbox commands, files and project memory run directly. Plan review remains a separate interaction.

Cloud tasks continue without a connected computer. A task that needs the computer waits for its connection; open device settings or cancel the wait. Accepted operations synchronize their original receipts after reconnection. **Stop task** requests cancellation; closing the webpage does not stop computer tools.

Web tasks use a separate, visible browser. Pages remain open across tool calls, conversation turns, and brief disconnections. The client prefers a compatible system Chrome installation and prepares a browser automatically when needed. Website sign-ins are stored separately for each Studio account, project, and conversation; the client does not read your everyday browser profile. Switching accounts closes the previous account's browser and preserves its sign-ins. Stopping a browser operation closes that conversation's browser and preserves its sign-ins. Webpage actions and screenshots require neither Accessibility nor Screen Recording permission. Requests to open a link use the system's default browser.

Inspecting controls, clicking, and typing in other applications require Accessibility permission; capturing their windows also requires Screen Recording permission. Manage these under the client's **Permissions** page. Window content and images are passed through Studio to the selected model. Desktop applications use their own permissions and are not restricted by the sandbox rules for file and command tools.

Ordinary network access requires no domain configuration. Tool permissions come from local configuration, and MCP runs only configured programs. Edit `~/.tinkerfin/settings.json` directly when you need to change these settings. Model provider keys are not written there.

Stopping a task or revoking authorization cannot undo completed actions. Check files and application state before repeating offline work or an operation marked as uncertain. Uncertain operations are not automatically repeated.

See the [client guide](../../../apps/desktop/README.md) for installation, builds, data locations and removal.
