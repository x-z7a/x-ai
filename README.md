# x-ai

## AI Integration 

### Github Copilot
put this into your `mcp.json` file under `.vscode` folder
```
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
### Claude Desktop

#### Prerequisite
```
uv tool install mcp-proxy
```

>NOTE: Mac\Linuxe
```
uv tool update-shell
```

configuration:
```
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

> NOTE: if you run claude desktop from another machine, replace 127.0.0.1 with your XPlane machines' IP address.