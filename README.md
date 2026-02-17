# x-ai

## 1. Installation

1. Download the latest ZIP from the [Releases](#) page.
2. Unzip the contents.
3. Move the extracted folder into your X-Plane `plugins` directory.

---

## 2. AI Integration

### 2.1 GitHub Copilot

To enable integration with GitHub Copilot, add the following to your `.vscode/mcp.json` file:

```json
{
	"servers": {
		"xplane MCP": {
			"url": "http://127.0.0.1:8765/sse",
			"type": "sse"
		}
	},
	"inputs": []
}
```

---

### 2.2 Claude Desktop

#### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)

- Install the `mcp-proxy` tool:

```sh
uv tool install mcp-proxy
```

**Note (Mac/Linux):**  
Update your shell environment after installation:

```sh
uv tool update-shell
```

#### Configuration

Add the following configuration to your Claude Desktop settings:

```json
{
	"mcpServers": {
		"xplane": {
			"command": "/Users/dzou/.local/bin/mcp-proxy",
			"args": [
				"http://127.0.0.1:8765/sse"
			]
		}
	}
}
```

> **Tip:**  
> If you are running Claude Desktop from a different machine, replace `127.0.0.1` with the IP address of your X-Plane machine.

---