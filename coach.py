"""
LeetCode -> real-world abstraction scenario coach.

Reuses the GitHub Copilot CLI SDK session pattern from
youtube-digest/scripts/copilot_proxy.py (CopilotClient, enterprise subscription,
no separate API key), and attaches the community leetcode-mcp-server
(https://github.com/jinzcdev/leetcode-mcp-server) as an in-session MCP tool
so the model looks up/searches problems itself — no custom LeetCode API wrapper.

The "what am I preparing for" requirement lives entirely in profiles/*.yaml.
Adding a new target (e.g. a different job direction) means adding a new
profile file; this script and the MCP wiring stay unchanged.

Setup (one-time, same as youtube-digest):
  python -m copilot download-runtime
  copilot login        # OR: gh auth login
Node.js/npx must be available (used to run leetcode-mcp-server via npx -y).

Usage:
  python coach.py --profile ai_infra_inference_serving --slug lru-cache
  python coach.py --profile ai_infra_inference_serving --tags sliding-window,hash-table --limit 5
"""

import argparse
import asyncio
import json
import os
import re
import urllib.parse
from datetime import date
from pathlib import Path

import httpx
import yaml
from copilot import CopilotClient
from copilot.session import PermissionHandler
from copilot.session_events import AssistantMessageData, SessionIdleData

ROOT = Path(__file__).parent
PROFILES_DIR = ROOT / "profiles"
OUTPUT_DIR = ROOT / "output"


# Load .env from project root (same convention as youtube-digest/scripts/copilot_proxy.py)
def _load_env(path: Path):
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except FileNotFoundError:
        pass


_load_env(ROOT / ".env")

DEFAULT_MODEL = os.getenv("COPILOT_DEFAULT_MODEL", "claude-sonnet-5")
LEETCODE_SESSION = os.getenv("LEETCODE_SESSION", "")  # optional, only needed for private data
OBSIDIAN_BASE_URL = os.getenv("OBSIDIAN_BASE_URL", "http://127.0.0.1:27123")
OBSIDIAN_API_KEY = os.getenv("OBSIDIAN_API_KEY", "")
OBSIDIAN_VAULT_FOLDER = os.getenv("OBSIDIAN_VAULT_FOLDER", "LeetCode Scenarios")

_PROBLEM_SCHEMA = """{
  "problem": {"id": str, "title": str, "slug": str, "difficulty": str, "tags": [str], "url": str},
  "pattern": str,
  "scenarios": [
    {
      "domain": str,
      "situation": str,
      "constraint": str,
      "abstraction_bridge": str
    }
  ]
}"""

OUTPUT_SCHEMA = f"""
严格只输出 JSON（不要 markdown 代码块、不要任何解释文字）。
"problem.id" 必须是 LeetCode 官方题号（questionFrontendId，如 "136"），"problem.url" 必须是可直接打开的题目链接（如 "https://leetcode.com/problems/single-number/"）。
单道题目（按 slug 查询）时输出一个对象，遵循这个 schema：
{_PROBLEM_SCHEMA}
多道题目（按 tags 搜索）时输出一个 JSON 数组，数组每一项都遵循上面同样的 schema，不要再包一层外壳对象。
"""


def load_profile(name: str) -> dict:
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.exists():
        available = [p.stem for p in PROFILES_DIR.glob("*.yaml")]
        raise SystemExit(f"Unknown profile '{name}'. Available: {available}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def build_system_prompt(profile: dict) -> str:
    domains = "\n".join(f"  - {d}" for d in profile.get("scenario_domains", []))
    prompt = profile["system_prompt"].replace("{scenario_domains}", "\n" + domains)
    return prompt.strip() + "\n" + OUTPUT_SCHEMA


def build_mcp_config() -> dict:
    env = {"LEETCODE_SESSION": LEETCODE_SESSION} if LEETCODE_SESSION else {}
    return {
        "leetcode": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@jinzcdev/leetcode-mcp-server", "--site", "global"],
            "env": env,
            "tools": ["*"],
        }
    }


def build_task_prompt(profile: dict, slug: str | None, tags: list[str], limit: int) -> str:
    if slug:
        lookup = f'调用 leetcode MCP 的 get_problem 工具查询 titleSlug="{slug}" 的题目详情。'
    else:
        tag_list = tags or profile.get("focus_tags", [])
        lookup = (
            f"调用 leetcode MCP 的 search_problems 工具，tags={tag_list}，"
            f"limit={limit}，从结果中选出最具代表性的题目逐一分析。"
        )
    return f"{lookup}\n\n对每道题目完成一次「真实场景 -> 抽象 -> 该题目」的逆向还原分析。"


async def run(profile_name: str, slug: str | None, tags: list[str], limit: int):
    profile = load_profile(profile_name)
    system = build_system_prompt(profile)
    prompt = build_task_prompt(profile, slug, tags, limit)

    client = CopilotClient()
    await client.start()
    session = await client.create_session(
        on_permission_request=PermissionHandler.approve_all,
        model=DEFAULT_MODEL,
        system_message={"mode": "replace", "content": system},
        mcp_servers=build_mcp_config(),
    )
    result: dict = {}
    done = asyncio.Event()

    def on_event(event):
        match event.data:
            case AssistantMessageData() as data:
                result["text"] = data.content
            case SessionIdleData():
                done.set()

    session.on(on_event)
    try:
        await session.send(prompt)
        await done.wait()
    finally:
        await client.delete_session(session.session_id)
        await client.stop()

    text = result.get("text", "")
    parsed = parse_json(text)
    label = slug or "search"
    save_local(profile_name, label, parsed, text)
    for entry in _as_problem_list(parsed):
        await save_to_obsidian(profile_name, entry)


def parse_json(text: str):
    # strip accidental ```json fences before parsing
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _as_problem_list(parsed) -> list[dict]:
    # search mode returns a JSON array (one entry per problem); slug mode returns a single object
    if isinstance(parsed, list):
        return [p for p in parsed if isinstance(p, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def save_local(profile_name: str, label: str, parsed, raw_text: str):
    out_dir = OUTPUT_DIR / profile_name
    out_dir.mkdir(parents=True, exist_ok=True)
    if parsed is not None:
        path = out_dir / f"{label}-{date.today().isoformat()}.json"
        path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path = out_dir / f"{label}-{date.today().isoformat()}.txt"
        path.write_text(raw_text, encoding="utf-8")
    print(f"\n✓ saved {path}\n")
    print(raw_text)


# ── Obsidian export (same REST API pattern as copilot_proxy.py, different vault folder) ──

def _format_scenario_md(profile_name: str, parsed: dict) -> str:
    problem = parsed.get("problem", {})
    title = problem.get("title", "")
    pid = problem.get("id", "")
    url = problem.get("url", "")
    tags = problem.get("tags", [])
    out = [
        "---\n",
        "type: leetcode-scenario-coach\n",
        f"profile: {profile_name}\n",
        f"leetcode_id: {pid}\n",
        f"slug: {problem.get('slug', '')}\n",
        f"url: {url}\n",
        f"difficulty: {problem.get('difficulty', '')}\n",
        f"tags: [{', '.join(tags)}]\n",
        f"saved_at: {date.today().isoformat()}\n",
        "---\n\n",
        f"# {pid}. {title} · {problem.get('difficulty', '')}\n\n",
    ]
    if url:
        out.append(f"[Open on LeetCode]({url})\n\n")
    if parsed.get("pattern"):
        out.append(f"**Pattern**: {parsed['pattern']}\n\n")
    out.append("## Scenarios\n\n")
    for sc in parsed.get("scenarios", []):
        out.append(f"### {sc.get('domain', '')}\n\n")
        out.append(f"**Situation**: {sc.get('situation', '')}\n\n")
        out.append(f"**Constraint**: {sc.get('constraint', '')}\n\n")
        out.append(f"**Abstraction bridge**: {sc.get('abstraction_bridge', '')}\n\n")
    return "".join(out)


def _obsidian_headers() -> dict:
    return {"Authorization": f"Bearer {OBSIDIAN_API_KEY}"}


def _vault_url(path: str) -> str:
    return f"{OBSIDIAN_BASE_URL}/vault/{urllib.parse.quote(path, safe='/')}"


async def save_to_obsidian(profile_name: str, entry: dict):
    if not OBSIDIAN_API_KEY:
        return
    problem = entry.get("problem", {})
    label = problem.get("slug") or problem.get("title") or "untitled"
    md = _format_scenario_md(profile_name, entry)
    safe_label = re.sub(r'[\\/*?:"<>|#]', "", label)[:80].strip()
    path = f"{OBSIDIAN_VAULT_FOLDER}/{profile_name}/{safe_label}.md"
    async with httpx.AsyncClient() as client:
        r = await client.put(
            _vault_url(path),
            headers={**_obsidian_headers(), "Content-Type": "text/markdown"},
            content=md.encode("utf-8"),
            timeout=10.0,
        )
    if r.status_code not in (200, 204):
        print(f"✗ Obsidian save failed {r.status_code}: {r.text[:200]}")
        return
    print(f"✓ Obsidian: saved {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True, help="profile name under profiles/ (without .yaml)")
    ap.add_argument("--slug", help="a specific LeetCode titleSlug, e.g. lru-cache")
    ap.add_argument("--tags", default="", help="comma-separated tags, overrides profile.focus_tags")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    asyncio.run(run(args.profile, args.slug, tags, args.limit))


if __name__ == "__main__":
    main()
