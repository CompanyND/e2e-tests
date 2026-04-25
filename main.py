# -*- coding: utf-8 -*-
"""
JIRA -> Claude Playwright Test Generator Bot
=============================================
Nasazeni: Railway
Pozadavky: fastapi, httpx, uvicorn
"""

from __future__ import annotations

import os
import json
import base64
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
JIRA_BASE_URL      = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL         = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN     = os.environ.get("JIRA_API_TOKEN", "")
BB_OAUTH_CLIENT_ID     = os.environ.get("BB_OAUTH_CLIENT_ID", "")
BB_OAUTH_CLIENT_SECRET = os.environ.get("BB_OAUTH_CLIENT_SECRET", "")
BB_WORKSPACE       = os.environ.get("BB_WORKSPACE", "netdirect-custom-solution")
DEBUG_RUN          = os.environ.get("DEBUG_RUN", "").lower() == "true"

# ---------------------------------------------------------------------------
# Cache: JIRA project key -> Bitbucket e2e repo slug
# Resetuje se pri restartu — pro persistenci pouzij SQLite
# ---------------------------------------------------------------------------
_repo_cache: dict[str, str] = {}
_config_cache: dict[str, dict] = {}  # repo_slug -> e2e.config.json
_bb_token_cache: dict = {"token": None, "expires_at": 0.0}

import time

# ---------------------------------------------------------------------------
# Bitbucket OAuth
# ---------------------------------------------------------------------------

async def get_bb_token() -> str:
    import time
    if _bb_token_cache["token"] and time.time() < _bb_token_cache["expires_at"] - 60:
        return _bb_token_cache["token"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://bitbucket.org/site/oauth2/access_token",
            data={"grant_type": "client_credentials"},
            auth=(BB_OAUTH_CLIENT_ID, BB_OAUTH_CLIENT_SECRET),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _bb_token_cache["token"] = data["access_token"]
        _bb_token_cache["expires_at"] = time.time() + data.get("expires_in", 7200)
        print(f"[BB] Novy OAuth token ziskan")
        return _bb_token_cache["token"]


# ---------------------------------------------------------------------------
# e2e.config.json
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "urls": {"dev": "http://localhost:4200"},
}

async def get_e2e_config(repo_slug: str) -> dict:
    if repo_slug in _config_cache:
        return _config_cache[repo_slug]

    token = await get_bb_token()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo_slug}/src/main/e2e.config.json",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.is_success:
                config = resp.json()
                _config_cache[repo_slug] = config
                print(f"[BB] Nacten e2e.config.json pro {repo_slug}")
                return config
            else:
                print(f"[BB] e2e.config.json nenalezen (HTTP {resp.status_code}), pouzivam vychozi")
    except Exception as e:
        print(f"[BB] Chyba pri nacitani e2e.config.json: {e}")

    return DEFAULT_CONFIG


def resolve_component(config: dict, jira_components: list[str]) -> tuple[str, str]:
    """
    Zjisti slozku a DEV URL podle JIRA component.
    Vraci (folder, dev_url).
    """
    components_map = config.get("components", {})
    default = config.get("default_component", "")

    # Najdi prvni component z ticketu ktery je v configu
    component_name = None
    for c in jira_components:
        if c in components_map:
            component_name = c
            break

    # Fallback na default_component
    if not component_name:
        component_name = default
        if component_name:
            print(f"[Config] Pouzivam default_component: {component_name}")
        else:
            print(f"[Config] Zadny component nenalezen, pouzivam root slozku")
            # Pokud neni ani default, pouzij root config
            urls = config.get("urls", {})
            dev_url = next((urls[e] for e in ("dev", "test", "prod") if urls.get(e, "").strip()), "http://localhost:4200")
            return "", dev_url

    comp_config = components_map.get(component_name, {})
    folder = comp_config.get("folder", "")
    urls = comp_config.get("urls", {})
    dev_url = next((urls[e] for e in ("dev", "test", "prod") if urls.get(e, "").strip()), "http://localhost:4200")

    print(f"[Config] Component: {component_name} | folder: {folder} | url: {dev_url}")
    return folder, dev_url


# ---------------------------------------------------------------------------
# JIRA helpers
# ---------------------------------------------------------------------------

def jira_auth():
    return (JIRA_EMAIL, JIRA_API_TOKEN)


async def get_jira_issue(issue_key: str) -> dict:
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, auth=jira_auth(), timeout=15)
        resp.raise_for_status()
        return resp.json()


def get_ac_text(issue_data: dict) -> str:
    """Vytahne akceptacni kriteria z customfield_10207."""
    adf = issue_data.get("fields", {}).get("customfield_10207")
    if not adf:
        return ""
    if isinstance(adf, str):
        return adf
    texts = []
    def walk(node):
        if node.get("type") == "text":
            texts.append(node.get("text", ""))
        for child in node.get("content", []):
            walk(child)
    walk(adf)
    return "\n".join(t for t in texts if t.strip())


# ---------------------------------------------------------------------------
# Bitbucket repo slug — konvence: {project_key.lower()}-e2e-tests
# Priklad: PRE -> pre-e2e-tests, NDE -> nde-e2e-tests
# ---------------------------------------------------------------------------

def get_e2e_repo_slug(project_key: str) -> str:
    return f"{project_key.lower()}-e2e-tests"


# ---------------------------------------------------------------------------
# Claude - generovani Playwright testu
# ---------------------------------------------------------------------------

PLAYWRIGHT_SYSTEM_PROMPT = (
    "You are an expert QA engineer specializing in Playwright end-to-end tests. "
    "Generate a complete Playwright TypeScript test file (.spec.ts) based on acceptance criteria. "
    "Use the following structure:\n"
    "- import {{ test, expect }} from \'@playwright/test\'\n"
    "- One test() block per [SCENARIO]\n"
    "- [GIVEN] maps to test setup/navigation\n"
    "- [WHEN] maps to user actions (click, fill, etc.)\n"
    "- [THEN] and [AND] map to expect() assertions\n"
    "- Use realistic selectors (data-testid, role, label)\n"
    "- Add BASE_URL variable at the top\n"
    "- Output ONLY the TypeScript code, no markdown, no explanation."
)


async def generate_playwright_tests(issue_key: str, summary: str, ac_text: str, dev_url: str = "http://localhost:4200") -> str:
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    user_prompt = (
        f"Generate Playwright TypeScript tests for JIRA ticket {issue_key}: {summary}\n\n"
        f"Base URL for this project: {dev_url}\n"
        f"Use process.env.BASE_URL || '{dev_url}' at the top of the file.\n\n"
        f"Acceptance criteria:\n{ac_text}"
    )
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "system": PLAYWRIGHT_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        # Safety net — odstran markdown code fence pokud Claude pridal navzdory instrukci
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = lines[1:]  # odstran prvni radek s ```typescript
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]  # odstran posledni ```
            raw = "\n".join(lines)
        return raw


# ---------------------------------------------------------------------------
# Bitbucket - commit souboru
# ---------------------------------------------------------------------------

async def commit_playwright_test(repo_slug: str, issue_key: str, content: str, folder: str = "") -> bool:
    """Commitne .spec.ts soubor do e2e repo do spravne slozky."""
    token = await get_bb_token()
    # Sestaveni cesty: components/PRE-294.spec.ts nebo PRE-294.spec.ts
    filepath = f"{folder}/{issue_key}.spec.ts" if folder else f"{issue_key}.spec.ts"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo_slug}/src",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "message": f"feat: Playwright testy pro {issue_key} [auto-generated]",
                "branch": "main",
                "author": "E2E Test Agent <e2e-agent@netdirect.cz>",
                filepath: content,
            },
            timeout=30,
        )
        if resp.is_success:
            print(f"[BB] Commitnuto: {filepath} do {repo_slug}")
            return True
        else:
            print(f"[BB] Chyba commitu: {resp.status_code} {resp.text[:200]}")
            return False


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.body()
        print(f"[DEBUG] Raw body: {body[:500]}")
        payload = json.loads(body)
    except Exception as e:
        raise HTTPException(400, f"Neplatny JSON: {e}")

    issue_key    = payload.get("issueKey", "")
    triggered_by = payload.get("triggeredBy", "neznamy")

    if not issue_key:
        raise HTTPException(400, "Chybi issueKey")

    print(f"[Webhook] {issue_key} | spustil: {triggered_by}")

    # Stahni ticket z JIRA
    issue_data = await get_jira_issue(issue_key)
    fields     = issue_data.get("fields", {})
    summary    = fields.get("summary", "")
    project_key = issue_key.split("-")[0]
    jira_components = [c.get("name", "") for c in fields.get("components", [])]
    print(f"[JIRA] Components: {jira_components if jira_components else 'zadne'}")

    # Vytahni AK z custom fieldu
    ac_text = get_ac_text(issue_data)
    if not ac_text:
        print(f"[ERROR] Ticket {issue_key} nema AK — nejdrive vygeneruj akceptacni kriteria")
        raise HTTPException(400, "Ticket nema akceptacni kriteria. Nejdrive spust Vygenerovat AK.")

    print(f"[PW] AK nalezena ({len(ac_text)} znaku) | Hledam e2e repo...")

    # Repo slug — konvence: pre-e2e-tests, nde-e2e-tests atd.
    repo_slug = get_e2e_repo_slug(project_key)
    print(f"[BB] E2E repo: {repo_slug}")

    # Nacti e2e config a zjisti component
    e2e_config = await get_e2e_config(repo_slug)
    folder, dev_url = resolve_component(e2e_config, jira_components)
    print(f"[PW] Slozka: {folder or 'root'} | DEV URL: {dev_url}")

    if DEBUG_RUN:
        print(f"[DEBUG_RUN] Preskakuji Claude + commit | repo: {repo_slug} | folder: {folder} | url: {dev_url}")
        return JSONResponse({"status": "debug_run", "repo": repo_slug, "folder": folder, "dev_url": dev_url})

    # Generuj Playwright testy
    print(f"[PW] Generuji Playwright testy pro {issue_key}...")
    test_code = await generate_playwright_tests(issue_key, summary, ac_text, dev_url)
    print(f"[PW] Vygenerovano {len(test_code)} znaku kodu")

    # Commitni do Bitbucketu do spravne slozky
    success = await commit_playwright_test(repo_slug, issue_key, test_code, folder)
    if not success:
        raise HTTPException(500, "Chyba pri commitu do Bitbucketu")

    return JSONResponse({
        "status": "ok",
        "issue_key": issue_key,
        "repo": repo_slug,
        "file": f"{issue_key}.spec.ts",
        "test_length": len(test_code),
    })


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "anthropic": "ok" if ANTHROPIC_API_KEY else "missing",
        "jira": "ok" if all([JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN]) else "missing",
        "bitbucket": "ok" if all([BB_OAUTH_CLIENT_ID, BB_OAUTH_CLIENT_SECRET]) else "missing",
        "workspace": BB_WORKSPACE,
    }
