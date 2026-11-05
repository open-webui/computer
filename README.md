# Open WebUI Computer

![GitHub stars](https://img.shields.io/github/stars/open-webui/computer?style=social)
![GitHub forks](https://img.shields.io/github/forks/open-webui/computer?style=social)
![GitHub watchers](https://img.shields.io/github/watchers/open-webui/computer?style=social)
![GitHub repo size](https://img.shields.io/github/repo-size/open-webui/computer)
![GitHub language count](https://img.shields.io/github/languages/count/open-webui/computer)
![GitHub top language](https://img.shields.io/github/languages/top/open-webui/computer)
![GitHub last commit](https://img.shields.io/github/last-commit/open-webui/computer?color=red)
[![Discord](https://img.shields.io/badge/Discord-Open_WebUI-blue?logo=discord&logoColor=white)](https://discord.gg/5rJgQTnV4s)
[![](https://img.shields.io/static/v1?label=Sponsor&message=%E2%9D%A4&logo=GitHub&color=%23fe8e86)](https://github.com/sponsors/open-webui)

![Open WebUI Computer Demo](./demo.png)

Open WebUI Computer (`cptr`) runs on your machine and serves your whole computer to any browser: files, terminal, editor, git, browser tabs, running sessions, AI agents, and tools. It literally is your computer.

Use it from your phone, tablet, laptop, another computer, or the machine it's running on. Designed to feel native on every screen. Connect your own AI via API key, plug in a coding agent you already subscribe to, or work directly in the terminal. One tool, full workstation, any device.

> Start here: [Open WebUI Computer docs](https://docs.openwebui.com/ecosystem/computer/)

### Install

```bash
pip install cptr
cptr run
```

MCP tool servers require the optional MCP dependencies: `pip install 'cptr[mcp]'`.
To install every optional feature group, use `pip install 'cptr[all]'`.
The Docker image includes all optional feature groups.

On Windows, if opening a terminal reports a missing `VCRUNTIME140.dll` or Universal CRT DLL, install Microsoft's [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) and restart `cptr`.

Opens in your browser at `http://localhost:8000`.

### Run with uv

```bash
uvx cptr@latest run --host 0.0.0.0
```

### Access from your phone

Same Wi-Fi? Bind to all interfaces:

```bash
cptr run --host 0.0.0.0
```

Open `http://<your-computer-ip>:8000` on your phone.

Not on the same network? Use a tunnel to reach your machine remotely:

- **[Tailscale](https://tailscale.com)** creates a private mesh network between your devices. Recommended.
- **[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)** gives you a permanent URL through Cloudflare's edge.
- **[ngrok](https://ngrok.com)** gives you a public URL in one command.

Most tunnels forward to `localhost`, so the default `cptr run` works. If your tunnel connects to a specific interface, bind accordingly with `--host`.

Or skip networking entirely and connect a [messaging bot](#messaging-bots) instead.

### The Whole Machine

Open WebUI Computer is the real workstation surface: files, shell, git state, workspaces, chats, tools, and sessions stay together wherever you open it.

| | |
|---|---|
| 📁 **Real files** | Navigate, create, rename, upload, drag and drop. Icons by type, sizes at a glance. |
| ✏️ **Editor** | Syntax-highlighted editing with tabs. Open multiple files side by side. |
| 🔀 **Git** | Stage, commit, diff, branch, push. Visual changes view. No command line required. |
| ⌨️ **Terminal** | Full shell in the browser. Run your tools, your scripts, or your favourite coding agent. |
| 🌐 **Browser** | Browse with Proxy, a private Managed Chrome profile, or your own Personal Chrome session. |
| 🔄 **Sessions persist** | Terminal keeps running when you close the tab. Come back on any device. |
| 🗂️ **Tabs** | Open browsers, terminals, files, chats, and tools in separate tabs. Rearrange or split your layout. |
| 📂 **Workspaces** | Multiple projects, one instance. Switch without losing your place. |
| 🔍 **Search** | Find files by name or contents, then jump straight to a matching line. ⌘K also searches chat history. |
| 📱 **Mobile-first** | Not a desktop UI made smaller. Built for the screen in your pocket. |

### AI agent

AI that works where your work actually lives.

Bring your own API key (OpenAI, Anthropic, Ollama, or any OpenAI-compatible endpoint), or connect a coding agent you already subscribe to. It can read the workspace, edit files, run commands, browse the web, use tools, automate recurring work, and spin up parallel sub-agents on the same machine.

| | |
|---|---|
| 💬 **Chat** | Built-in AI with streaming responses and tool calling. Not just conversation: it can act. |
| 🎙️ **Voice mode** | Talk to your computer hands-free. It listens, responds, and reads the answer back to you. The mic re-arms so you can have a real conversation. |
| 🔊 **Text-to-speech** | AI responses read aloud, sentence by sentence. Works with any OpenAI-compatible TTS API. |
| 💭 **Reasoning** | See what the AI is thinking. Models like o3 and Claude show their chain of thought as expandable sections. |
| 🔧 **File tools** | AI reads, writes, edits, and searches your codebase directly. |
| ▶️ **Run commands** | AI executes shell commands and reads the output. Foreground or background. |
| 🌐 **Web browsing** | Navigate pages, click elements, fill forms, take screenshots. |
| 🔍 **Web search** | Brave, DuckDuckGo, Exa, Tavily, Perplexity, or any chat completions endpoint. |
| 🖼️ **Image understanding** | AI reads and describes images and screenshots from your workspace. |
| 📋 **Plan mode** | Request an implementation plan before the AI writes a single line. |
| ✏️ **Output editing** | Review and edit AI-generated changes before applying. |
| 📎 **File mentions** | Type `@` to give the AI context about specific files. |
| 🧩 **Skills** | Reusable instruction sets (SKILL.md files). Type `$` to mention one. |
| ⏱️ **Scheduled** | Schedule recurring AI tasks. "Run tests every morning." "Deploy every Friday." |
| 🤖 **Sub-agents** | AI spins up parallel workers for complex tasks. Each gets full tool access. |
| 🔌 **Tool servers** | Connect external tools via MCP or OpenAPI. |
| 🧠 **Context compaction** | Long conversations are automatically summarised to stay fast. |

### Coding agents

Use the subscriptions you already pay for as native backends on your own machine. No separate API key needed.

**Codex** · **Claude Code** · **Cursor** · **Grok** · **OpenCode** · **Cline** · **Gemini** · **Pi**

Add an agent profile from Settings, pick your models, and it shows up in the model selector like any other provider. Conversations run inside your workspace with full tool access and resume where you left off.

Prefer to run agents yourself? Any terminal agent (Kilo Code, Pi, and others) works in the terminal tab the way it always has.

### Messaging bots

Message your computer from wherever you are. Connect the AI to your chat apps with full tool access, streaming responses, and conversations synced back to the web UI.

**Telegram** · **Discord** · **Slack** · **WhatsApp** · **Signal**

Ask it to check a build, push a fix, or explain a file. Switch workspaces with `/workspace`, start fresh with `/new`.

### Gateway API

Turn each workspace into an OpenAI-compatible agent model with real machine access. Open WebUI Computer exposes `/v1/chat/completions`, so any client that speaks OpenAI, including [Open WebUI](https://github.com/open-webui/open-webui), can use a workspace with full agent capabilities: file access, terminal, web search, tools.

### More

| | |
|---|---|
| 🎙️ **Voice memos** | Record audio, auto-transcribe to markdown. |
| 💬 **Message queue** | Queue follow-up messages while the AI is responding. |
| 🔔 **Notifications** | Browser notifications plus named webhook or bot targets for chat events. |
| 📊 **Usage** | Token counts and timing on every response. |
| 📄 **System prompts** | Per-model, per-workspace, or global. Template variables included. |
| 📋 **Audit logging** | Structured audit trail of all API mutations with automatic redaction of sensitive data. |
| 🪵 **Diagnostic logging** | Configurable structured logs (text or JSON) with optional upstream request capture. |
| ⌨️ **Keyboard shortcuts** | Customisable keybindings with a settings panel. |
| 🌍 **10 languages** | English, Deutsch, Español, Français, Português (Brasil), Русский, 日本語, 한국어, 简体中文, 繁體中文. |
| 🔐 **Auth** | Username/password with JWT sessions. Signup toggle for admins. |

### Design principles

**Mobile is first-class.** The interface is built for the phone. Touch-native, portrait-native, designed for the screen people carry. Sessions survive disconnects because on a phone, they will. If a feature only works at a desk, it's not done.

**Your machine.** Open WebUI Computer serves the machine it runs on. The local filesystem, the local shell, local state. Where that machine lives is up to you.

**Computer, not chat.** The core is the filesystem, the terminal, and git. Files over apps. Plain files on your machine, not content trapped inside another product. AI conversations are files too: searchable, editable, movable, commit-able. Open WebUI Computer is a window into the real system, not a container on top of it.

Open WebUI Computer runs on your machine and puts the whole thing in a browser tab. Pull out your phone and you're in. Files, editor, terminal, git, running on the computer you already own.

Push a hotfix from the train. Check on a deploy from bed. Ship a side project from the park. Stage and commit without touching the command line, or open the terminal and do it the old way. Search across files. Preview markdown. Drag things around. Switch between projects without losing your place.

Close the tab. Come back tomorrow on any device. Everything is where you left it. Sessions survive disconnects. Your work doesn't care which screen you're on.

Life is short. Touch grass. Read our [Manifesto](MANIFESTO.md).

> Help us build open, empowering tools that put AI in people's hands.
> [We're hiring](http://careers.openwebui.com/).

### Docker

Run Open WebUI Computer with Docker:

```bash
docker run --rm -it \
  -p 8000:8000 \
  -v cptr-data:/data \
  -v "$PWD:/workspace" \
  -w /workspace \
  ghcr.io/open-webui/computer:latest
```

Then open the URL printed in the logs, usually `http://localhost:8000/?token=...`.

Open WebUI Computer stores its state in `/data`. Mount your project into the container, like `-v "$PWD:/workspace"`, so Open WebUI Computer can access it.
The default image includes Git and GitHub CLI (`gh`). Use `ghcr.io/open-webui/computer:browser` when agent browser automation needs Chromium.

Non-streaming utility tasks also use the `CPTR_STREAM_*` timeout settings. For slow models, increase `CPTR_STREAM_READ_TIMEOUT` (default: 300 seconds), for example with `-e CPTR_STREAM_READ_TIMEOUT=14400` in `docker run`. Restart Computer after changing this setting; Open WebUI and reverse proxies may have their own timeouts.

If you bind-mount a host directory to `/data`, make sure that directory is writable by the container user. SQLite needs to create and update `/data/app.db`, and host directory permissions take precedence over the image's built-in `/data` ownership.

To connect a host-running OpenCode server from Docker, start OpenCode on a host-reachable interface and set the OpenCode agent Server URL to the Docker host address:

```bash
opencode serve --hostname 0.0.0.0 --port 4096
```

Use `http://host.docker.internal:4096` on Docker Desktop. On Linux, add `--add-host=host.docker.internal:host-gateway` to `docker run` if that hostname is not already available.

The `:dev` image is also available and tracks the `main` branch.

### Air-gapped installation

Open WebUI Computer does not need internet access after it is installed. The Python wheel includes the built frontend assets, and the Docker image is self-contained.

On a connected machine:

```bash
pip download --dest wheelhouse 'cptr[all]'
docker pull ghcr.io/open-webui/computer:latest
docker save ghcr.io/open-webui/computer:latest -o cptr-image.tar
```

Transfer the artifacts, then install or run offline:

```bash
python -m venv .venv
. .venv/bin/activate
pip install --no-index --find-links ./wheelhouse 'cptr[all]'
cptr run --host 0.0.0.0

docker load -i cptr-image.tar
docker run --rm -it \
  --network=none \
  -p 8000:8000 \
  -v cptr-data:/data \
  -v "$PWD:/workspace" \
  -w /workspace \
  ghcr.io/open-webui/computer:latest
```

Core local features run from local assets. External services such as hosted model APIs, web search providers, messaging adapters, Git remotes, and MCP/OpenAPI servers still require reachable endpoints.

### Security model

Open WebUI Computer is designed as **your computer, served to you**. Once authenticated, a user has full access to the host filesystem and shell, equivalent to an SSH session. There is no path sandboxing and no per-user isolation.

This is safe when you are the only user and you control the network. It is not safe if untrusted users share the instance, it is exposed to the public internet, or a reverse proxy forwards spoofable auth headers. Treat a shared Open WebUI Computer like an open SSH port.

### Contributing

We don't take pull requests. [Open an issue](https://github.com/open-webui/computer/issues) describing the bug or idea.

### License

Open Use License. Source available. All rights reserved. See [LICENSE](LICENSE). [Commercial licenses](https://openwebui.com/computer/license) and [enterprise licenses](mailto:sales@openwebui.com) available.
