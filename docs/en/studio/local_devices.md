# Run local tasks on a Mac

[Studio quickstart](quick_start.md) · [简体中文](../../cn/studio/local_devices.md)

The TinkerFin client lets Studio conversations use files, Python, Node, an isolated browser and MCP tools on your Mac. The Agent handles the conversation in the cloud while the client executes local tools; model provider keys stay in the cloud.

## Connect

1. Open TinkerFin on an Apple Silicon Mac running macOS 15 or later.
2. Select **Connect** from its menu bar panel, then sign in to Studio in your browser and authorize the connection. After authorization succeeds, close the page and open the client from the menu bar.
3. Once the client shows **Connected**, select your Studio project and click the laptop icon in the header to start a local conversation.

The client creates project workspaces automatically. No directory selection is required. Manage the connection in **Settings → Local devices**. Once a device has been connected, the header shows a laptop icon: green when connected, red when disconnected.

Each account has one authorized device. A new authorization invalidates the previous pairing, and new messages in existing local conversations use the current device. Pending approvals retain their original authorization and never move automatically to another computer. In the client's **Settings**, click the device name to edit it; pressing Enter or leaving the field saves it automatically. Select **Switch** to change accounts.

Choose **Follow System**, **简体中文** or **English** under **Settings → Language**. Changes apply immediately and remain selected after restarting the client.

## Projects and files

Local workspaces live under `~/.tinkerfin/projects/`, isolated by Studio service, account and project identity. The cloud retains project ownership. Renaming a project or reconnecting the same account does not change its local directory. Switching accounts preserves existing directories, which are reused when you return. Moving to another computer does not transfer files automatically.

Cloud and local project files are independent. Tasks can import conversation attachments, and explicitly delivered results return as attachments for preview or download. In local mode, the workspace button opens the project directory through the client.

The project directory is the task's default working directory. Specify an absolute native path or a `~/` path to work with files elsewhere; relative paths resolve from the project directory. No per-directory authorization is required. Client settings, task state, credentials, and managed directories for other accounts or projects remain protected, and system permissions still apply.

Started conversations retain their execution location, and local conversations cannot be moved to another project. To change location or project, create a new conversation in the target project, then select cloud or local. Switching location in a new conversation preserves the text draft and revalidates attachments; references that cannot be reused must be added again. New conversations remember the project's last explicit location choice.

## Permissions and recovery

**Full access** and **Write approval** can reach the same local files. With **Write approval** selected, commands, file changes, browser and MCP actions request approval in the conversation. Cloud and local execution share the same conversation interface: approvals appear in the composer, results return to the conversation, and **Stop task** requests cancellation. Closing the webpage does not stop local tools. Reconnecting after a brief device disconnection resumes synchronization of accepted operations. If an extended disconnection causes the request to fail, reconnect and check the result before continuing.

Web tasks use a separate, visible browser. Pages remain open across tool calls, conversation turns, and brief disconnections. The client prefers a compatible system Chrome installation and prepares a browser automatically when needed. Website sign-ins are stored separately for each Studio account, project, and conversation; the client does not read your everyday browser profile. Switching accounts closes the previous account's browser and preserves its sign-ins. Stopping a browser operation closes that conversation's browser and preserves its sign-ins. Webpage actions and screenshots require neither Accessibility nor Screen Recording permission. Requests to open a link use the system's default browser.

Inspecting controls, clicking, and typing in other applications require Accessibility permission; capturing their windows also requires Screen Recording permission. Manage these under the client's **Settings → Permissions**. Window content and images are passed through Studio to the selected model. Desktop applications use their own permissions and are not restricted by the sandbox rules for file and command tools.

Ordinary network access requires no domain configuration. Tool permissions come from local configuration, and MCP runs only configured programs. **Settings → Configuration** opens `~/.tinkerfin/settings.json`. Model provider keys are not written there.

Stopping a task or revoking authorization cannot undo completed actions. Check files and application state before repeating offline work or an operation marked as uncertain. Uncertain operations are not automatically repeated.

See the [client guide](../../../apps/desktop/README.md) for builds, data locations and removal.
