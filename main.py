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
# Bitbucket repo discovery s cache
# ---------------------------------------------------------------------------

async def find_e2e_repo(project_key: str) -> tuple[str, str] | None:
    """
    Najde nebo vytvori e2e-tests repo pro dany JIRA projekt key.
    Vraci (bb_project_key, repo_slug) nebo None.
    Cache: project_key -> "BB_PROJECT/repo_slug"
    """
    if project_key in _repo_cache:
        cached = _repo_cache[project_key]
        bb_project, repo_slug = cached.split("/", 1)
        print(f"[Cache] {project_key} -> {bb_project}/{repo_slug}")
        return bb_project, repo_slug

    print(f"[BB] Hledam e2e repo pro projekt {project_key}...")
    token = await get_bb_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        # Projdi vsechny BB projekty v workspace a hledej ten ktery odpovida JIRA key
        resp = await client.get(
            f"https://api.bitbucket.org/2.0/teams/{BB_WORKSPACE}/projects",
            headers=headers,
            timeout=15,
        )
        if not resp.is_success:
            # Zkus novejsi endpoint
            resp = await client.get(
                f"https://api.bitbucket.org/2.0/workspaces/{BB_WORKSPACE}/projects",
                headers=headers,
                timeout=15,
            )

        projects = resp.json().get("values", [])
        print(f"[BB] Nalezeno {len(projects)} BB projektu")

        # Hledej BB projekt kde key obsahuje JIRA project key
        bb_project_key = None
        for p in projects:
            if project_key.upper() in p.get("key", "").upper() or project_key.upper() in p.get("name", "").upper():
                bb_project_key = p.get("key")
                print(f"[BB] Nalezen BB projekt: {p.get('name')} (key: {bb_project_key})")
                break

        if not bb_project_key:
            print(f"[BB] BB projekt pro JIRA key {project_key} nenalezen")
            return None

        # Hledej e2e-tests repo v tomto BB projektu
        repos_resp = await client.get(
            f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}",
            headers=headers,
            params={"q": f'project.key="{bb_project_key}" AND name="e2e-tests"'},
            timeout=15,
        )
        repos = repos_resp.json().get("values", [])

        if repos:
            repo_slug = repos[0]["slug"]
            print(f"[BB] Nalezeno e2e repo: {repo_slug}")
        else:
            # Repo neexistuje -> vytvor ho
            print(f"[BB] e2e-tests repo neexistuje, vytvarim...")
            create_resp = await client.post(
                f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/e2e-tests-{project_key.lower()}",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "scm": "git",
                    "is_private": True,
                    "project": {"key": bb_project_key},
                    "name": "e2e-tests",
                },
                timeout=15,
            )
            if not create_resp.is_success:
                print(f"[BB] Chyba pri vytvareni repo: {create_resp.text}")
                return None
            repo_slug = create_resp.json()["slug"]
            print(f"[BB] Vytvoreno repo: {repo_slug}")

        # Uloz do cache
        _repo_cache[project_key] = f"{bb_project_key}/{repo_slug}"
        return bb_project_key, repo_slug


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


async def generate_playwright_tests(issue_key: str, summary: str, ac_text: str) -> str:
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    user_prompt = (
        f"Generate Playwright TypeScript tests for JIRA ticket {issue_key}: {summary}\n\n"
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
        return resp.json()["content"][0]["text"]


# ---------------------------------------------------------------------------
# Bitbucket - commit souboru
# ---------------------------------------------------------------------------

async def commit_playwright_test(repo_slug: str, issue_key: str, content: str) -> bool:
    """Commitne .spec.ts soubor do e2e repo."""
    token = await get_bb_token()
    filename = f"{issue_key}.spec.ts"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo_slug}/src",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "message": f"feat: Playwright testy pro {issue_key} [auto-generated]",
                "branch": "main",
                filename: content,
            },
            timeout=30,
        )
        if resp.is_success:
            print(f"[BB] Commitnuto: {filename} do {repo_slug}")
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

    # Vytahni AK z custom fieldu
    ac_text = get_ac_text(issue_data)
    if not ac_text:
        print(f"[ERROR] Ticket {issue_key} nema AK — nejdrive vygeneruj akceptacni kriteria")
        raise HTTPException(400, "Ticket nema akceptacni kriteria. Nejdrive spust Vygenerovat AK.")

    print(f"[PW] AK nalezena ({len(ac_text)} znaku) | Hledam e2e repo...")

    # Najdi e2e repo
    repo_result = await find_e2e_repo(project_key)
    if not repo_result:
        raise HTTPException(500, f"Nenalezeno e2e-tests repo pro projekt {project_key}")
    _, repo_slug = repo_result

    if DEBUG_RUN:
        print(f"[DEBUG_RUN] Preskakuji Claude + commit | repo: {repo_slug}")
        return JSONResponse({"status": "debug_run", "repo": repo_slug, "ac_length": len(ac_text)})

    # Generuj Playwright testy
    print(f"[PW] Generuji Playwright testy pro {issue_key}...")
    test_code = await generate_playwright_tests(issue_key, summary, ac_text)
    print(f"[PW] Vygenerovano {len(test_code)} znaku kodu")

    # Commitni do Bitbucketu
    success = await commit_playwright_test(repo_slug, issue_key, test_code)
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
