# leetcode-scenario-coach

Bridges the gap LeetCode doesn't cover: a problem is already abstracted away
from any real-world context, but real engineering (and real interviews) start
from a concrete situation and require *you* to do the abstraction. This tool
asks an LLM to reverse that process — given a LeetCode problem, it reconstructs
plausible real-world scenarios and the specific constraint that forces that
exact abstraction, scoped to whatever job direction you're preparing for.

## How it works

```
profile (profiles/*.yaml)  →  system prompt
LeetCode problem/tags       →  looked up via leetcode-mcp-server (MCP tool calls)
                            →  Copilot session reasons: situation → constraint → abstraction → problem
                            →  saved to output/*.json and (optionally) Obsidian
```

- **LLM access**: reuses the same `CopilotClient` session pattern as another
  local project of mine (`youtube-digest/scripts/copilot_proxy.py`) — a
  session against the GitHub Copilot CLI, so it runs on your existing Copilot
  subscription with no separate API key.
- **LeetCode data**: delegated entirely to
  [jinzcdev/leetcode-mcp-server](https://github.com/jinzcdev/leetcode-mcp-server),
  attached as an MCP server directly inside the Copilot session (`mcp_servers=`
  in `create_session`). The model calls its `get_problem` / `search_problems`
  tools itself — this project does not wrap or reimplement the LeetCode API.
- **Target direction is a profile, not code**: every "what am I preparing for"
  requirement lives in a `profiles/*.yaml` file (focus tags, candidate
  real-world domains, system prompt). Adding a new direction means adding a
  new profile file; `coach.py` and the MCP wiring stay unchanged.

## Setup

1. Node.js + npx (used to run `leetcode-mcp-server` via `npx -y`).
2. GitHub Copilot CLI, one-time:
   ```
   python -m copilot download-runtime
   copilot login        # OR: gh auth login
   ```
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` if you want scenario cards pushed to Obsidian
   via the [Local REST API](https://github.com/coddingtonbear/obsidian-local-rest-api)
   plugin (optional — without it, results are still saved under `output/`).

## Usage

```
python coach.py --profile ai_infra_inference_serving --slug lru-cache
python coach.py --profile ai_infra_inference_serving --tags sliding-window,hash-table --limit 5
```

## Credits

- [jinzcdev/leetcode-mcp-server](https://github.com/jinzcdev/leetcode-mcp-server) — the LeetCode MCP server this project relies on for all problem data.
- [GitHub Copilot SDK](https://www.npmjs.com/package/@github/copilot) — session/runtime used for LLM access.
