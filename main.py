# -*- coding: utf-8 -*-
"""
JIRA -> Claude Playwright Test Generator Bot
=============================================
Nasazeni: Railway
Pozadavky: fastapi, httpx, uvicorn
"""

from __future__ import annotations

import os
import re
import json
import base64
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

ANTHROPIC_API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")
JIRA_BASE_URL          = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL             = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN         = os.environ.get("JIRA_API_TOKEN", "")
BB_OAUTH_CLIENT_ID     = os.environ.get("BB_OAUTH_CLIENT_ID", "")
BB_OAUTH_CLIENT_SECRET = os.environ.get("BB_OAUTH_CLIENT_SECRET", "")
BB_WORKSPACE           = os.environ.get("BB_WORKSPACE", "netdirect-custom-solution")
DEBUG_RUN              = os.environ.get("DEBUG_RUN", "").lower() == "true"

_config_cache: dict[str, dict] = {}
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

DEFAULT_CONFIG = {"urls": {"dev": "http://localhost:4200"}}

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
            print(f"[BB] e2e.config.json nenalezen (HTTP {resp.status_code}), pouzivam vychozi")
    except Exception as e:
        print(f"[BB] Chyba pri nacitani e2e.config.json: {e}")
    return DEFAULT_CONFIG


def resolve_component(config: dict, jira_components: list[str]) -> tuple[str, str]:
    components_map = config.get("components", {})
    default = config.get("default_component", "")
    component_name = None
    for c in jira_components:
        if c in components_map:
            component_name = c
            break
    if not component_name:
        component_name = default
        if component_name:
            print(f"[Config] Pouzivam default_component: {component_name}")
        else:
            print(f"[Config] Zadny component, pouzivam root")
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
# JIRA dev-status + Bitbucket PR diff
# ---------------------------------------------------------------------------

async def get_linked_pr(issue_id: str) -> tuple[str, int] | None:
    url = f"{JIRA_BASE_URL}/rest/dev-status/1.0/issue/detail"
    params = {"issueId": issue_id, "applicationType": "bitbucket", "dataType": "pullrequest"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, auth=jira_auth(), timeout=15)
        if not resp.is_success:
            print(f"[JIRA] dev-status API chyba: {resp.status_code}")
            return None
        data = resp.json()
        prs = data.get("detail", [{}])[0].get("pullRequests", [])
        if not prs:
            print(f"[JIRA] Zadne linked PR nalezeny")
            return None
        merged = [pr for pr in prs if pr.get("status") == "MERGED"]
        if not merged:
            merged = prs
        pr = merged[0]  # nejnovejsi MERGED PR
        repo_name = pr.get("repositoryName", "")
        pr_id = pr.get("id")
        repo_slug = repo_name.split("/")[-1] if "/" in repo_name else repo_name
        print(f"[JIRA] Linked PR #{pr_id} v repo: {repo_slug}")
        return repo_slug, int(pr_id)


async def get_pr_diff_files(repo_slug: str, pr_id: int) -> list[tuple[str, str]]:
    token = await get_bb_token()
    MAX_CHARS = 8000
    files_content = []
    async with httpx.AsyncClient() as client:
        diff_resp = await client.get(
            f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo_slug}/pullrequests/{pr_id}/diffstat",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            follow_redirects=True,
        )
        if not diff_resp.is_success:
            print(f"[BB] diffstat chyba: {diff_resp.status_code}")
            return []
        files = diff_resp.json().get("values", [])
        print(f"[BB] PR #{pr_id} zmenil {len(files)} souboru")
        for f in files:
            filepath = f.get("new", {}).get("path") or f.get("old", {}).get("path", "")
            if not filepath:
                continue
            ext = filepath.split(".")[-1]
            if ext not in ("html", "ts", "cshtml", "razor", "cs") or filepath.endswith(".spec.ts"):
                continue
            # Preskoc .cs soubory ktere nejsou komponenty (napr. migrations, tests)
            if ext == "cs" and any(x in filepath.lower() for x in ("migration", "test", "spec", ".designer.")):
                continue
            try:
                src_resp = await client.get(
                    f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo_slug}/src/HEAD/{filepath}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                    follow_redirects=True,
                )
                if src_resp.is_success:
                    file_content = src_resp.text[:MAX_CHARS]
                    files_content.append((filepath, file_content))
                    print(f"[BB] Stazeno: {filepath} ({len(file_content)} znaku)")
            except Exception as e:
                print(f"[BB] Chyba pri stazeni {filepath}: {e}")
    return files_content


# ---------------------------------------------------------------------------
# Stack verze detekce (Angular, .NET)
# ---------------------------------------------------------------------------

async def _fetch_json_file(client: httpx.AsyncClient, repo_slug: str, token: str, path: str) -> dict | None:
    url = f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo_slug}/src/HEAD/{path}"
    resp = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if not resp.is_success:
        return None
    try:
        return resp.json()
    except Exception:
        return None


async def _list_dir(client: httpx.AsyncClient, repo_slug: str, token: str, path: str = "") -> list[dict]:
    all_values = []
    url = f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo_slug}/src/HEAD/{path}?pagelen=100"
    while url:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if not resp.is_success:
            break
        data = resp.json()
        all_values.extend(data.get("values", []))
        url = data.get("next")
    return all_values


async def detect_stack_versions(repo_slug: str, pr_id: int) -> dict:
    """Detekuje verze Angular a .NET — hledá v rootu i podslozkach."""
    token = await get_bb_token()
    versions = {"angular": "", "dotnet": ""}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Angular — root + podsložky (FlexMvc/, AdminMvc/ atd.)
        try:
            candidates = ["package.json"]
            root_files = await _list_dir(client, repo_slug, token)
            for f in root_files:
                if f.get("type") == "commit_directory":
                    candidates.append(f"{f['path']}/package.json")

            seen_versions = set()
            for path in candidates:
                pkg = await _fetch_json_file(client, repo_slug, token, path)
                if not pkg:
                    continue
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                version = deps.get("@angular/core", "")
                if not version:
                    continue
                match = re.search(r"(\d+)", version)
                if match:
                    seen_versions.add(match.group(1))

            if seen_versions:
                versions["angular"] = str(max(int(v) for v in seen_versions))
                print(f"[Stack] Angular v{versions['angular']} detekovan")
            else:
                versions["angular"] = "6"  # fallback pro starsi projekty
                print(f"[Stack] Angular verze nenalezena, pouzivam fallback v6")
        except Exception as e:
            print(f"[Stack] Chyba pri detekci Angular: {e}")

        # .NET — z .csproj souboru v rootu i podslozkach
        try:
            all_files = await _list_dir(client, repo_slug, token)
            csproj_paths = []
            for f in all_files:
                if f.get("path", "").endswith(".csproj"):
                    csproj_paths.append(f["path"])
                elif f.get("type") == "commit_directory":
                    sub_files = await _list_dir(client, repo_slug, token, f["path"])
                    for sf in sub_files:
                        if sf.get("path", "").endswith(".csproj"):
                            csproj_paths.append(sf["path"])

            for path in csproj_paths:
                resp = await client.get(
                    f"https://api.bitbucket.org/2.0/repositories/{BB_WORKSPACE}/{repo_slug}/src/HEAD/{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                if resp.is_success:
                    match = re.search(r"<TargetFramework>net(\d+)", resp.text)
                    if match:
                        versions["dotnet"] = match.group(1)
                        print(f"[Stack] .NET {versions['dotnet']} detekovan")
                        break
        except Exception as e:
            print(f"[Stack] Chyba pri detekci .NET: {e}")

    return versions


# ---------------------------------------------------------------------------
# Repo slug konvence
# ---------------------------------------------------------------------------

def get_e2e_repo_slug(project_key: str) -> str:
    return f"{project_key.lower()}-e2e-tests"


# ---------------------------------------------------------------------------
# Claude - generovani Playwright testu
# ---------------------------------------------------------------------------

PLAYWRIGHT_SYSTEM_PROMPT = (
    "You are an expert QA engineer specializing in Playwright end-to-end tests. "
    "Generate a complete Playwright TypeScript test file based on acceptance criteria. "
    "CRITICAL OUTPUT RULE: "
    "Your response must start EXACTLY with the character 'i' from 'import'. "
    "Do NOT include markdown, backticks, code fences, or explanations. "
    "STRUCTURE RULES: "
    "1. First line: import { test, expect } from '@playwright/test'; "
    "2. Second line: const BASE_URL = process.env.BASE_URL || 'PROVIDED_URL'; "
    "3. Use test.describe() per [SCENARIO]. "
    "4. [GIVEN]=page.goto()+setup. [WHEN]=user actions. [THEN]/[AND]=expect() assertions. "
    "5. For UI scenarios add mobile test.describe with page.setViewportSize({ width: 375, height: 667 }). "
    "6. Use test.beforeEach() for repeated setup. Never hardcode URLs. "
    "7. Add page.waitForLoadState('networkidle') after navigation. "
    "SELECTOR RULES (critical): "
    "8. Priority: getByTestId() > getByRole() > getByLabel() > getByPlaceholder() > locator(). "
    "9. For text matching use: page.getByText('text') or page.locator(':text(\"text\")'. "
    "10. NEVER use text*= or text~= inside locator() — invalid Playwright syntax. "
    "11. Use .filter({ hasText: 'text' }) NOT .filter({ has: locator('text*=...') }). "
    "12. For JSON-LD: page.locator('script[type=\"application/ld+json\"]') + page.evaluate(). "
    "13. Angular components: page.locator('app-breadcrumb'), page.locator('cmp-header'). "
    "14. CSS cannot match text — never use [text*=...] in CSS selectors. "
    "15. STRICT MODE: locator() must match exactly one element or use .first()/.nth(0) before any action. "
    "    NEVER call .evaluate() or assertions on a locator that resolves to multiple elements. "
    "16. For JSON-LD structured data use this exact pattern to avoid strict mode errors: "
    "    const scripts = await page.locator('script[type=\"application/ld+json\"]').all(); "
    "    const jsonData = await Promise.all(scripts.map(s => s.evaluate(el => { try { return JSON.parse(el.textContent || ''); } catch { return null; } }))); "
    "    const target = jsonData.find(d => d && d['@type'] === 'ExpectedType'); "
    "    expect(target).toBeDefined(); "
    "SOURCE CODE RULES: "
    "17. If source code provided: use ONLY real selectors (IDs, data-testid, Angular tags). "
    "18. NEVER invent selectors not in source code. "
    "19. If no source code: use semantic selectors and add // TODO: verify selector comments. "
    "TECH STACK RULES: "
    "20. If Angular version provided: use standalone components for v17+, NgModule for older. "
    "21. If .NET version provided: adjust API endpoint patterns accordingly. "
)


async def generate_playwright_tests(
    issue_key: str,
    summary: str,
    ac_text: str,
    dev_url: str = "http://localhost:4200",
    source_files: list[tuple[str, str]] | None = None,
    stack_versions: dict | None = None,
) -> str:
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    source_context = ""
    if source_files:
        parts = []
        for filepath, file_content in source_files:
            parts.append(f"=== {filepath} ===\n{file_content}")
        source_context = (
            "\n\nThe following Angular source files were changed in the PR for this ticket. "
            "Use them to extract REAL selectors, component structure, routes, and data bindings. "
            "Look for: HTML element IDs, data-testid attributes, Angular component selectors, "
            "router links, form control names, and any identifiers you can use in Playwright selectors.\n\n"
            + "\n\n".join(parts)
        )

    stack_info = ""
    if stack_versions:
        parts = []
        if stack_versions.get("angular"):
            parts.append(f"Angular v{stack_versions['angular']}")
        if stack_versions.get("dotnet"):
            parts.append(f".NET {stack_versions['dotnet']}")
        if parts:
            stack_info = f"\nTech stack: {', '.join(parts)}. Use version-appropriate patterns."

    user_prompt = (
        f"Generate Playwright TypeScript tests for JIRA ticket {issue_key}: {summary}\n\n"
        f"Base URL for this project: {dev_url}\n"
        f"Use process.env.BASE_URL || '{dev_url}' at the top of the file."
        f"{stack_info}\n\n"
        f"Acceptance criteria:\n{ac_text}"
        f"{source_context}"
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
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines)
        if "import" in raw and not raw.startswith("import"):
            raw = raw[raw.index("import"):]
        return raw


# ---------------------------------------------------------------------------
# Bitbucket - commit souboru
# ---------------------------------------------------------------------------

async def commit_playwright_test(repo_slug: str, issue_key: str, content: str, folder: str = "") -> bool:
    token = await get_bb_token()
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

    issue_data  = await get_jira_issue(issue_key)
    fields      = issue_data.get("fields", {})
    summary     = fields.get("summary", "")
    project_key = issue_key.split("-")[0]
    issue_id    = issue_data.get("id", "")
    jira_components = [c.get("name", "") for c in fields.get("components", [])]
    print(f"[JIRA] Components: {jira_components if jira_components else 'zadne'}")

    ac_text = get_ac_text(issue_data)
    if not ac_text:
        print(f"[ERROR] Ticket {issue_key} nema AK")
        raise HTTPException(400, "Ticket nema akceptacni kriteria. Nejdrive spust Vygenerovat AK.")

    print(f"[PW] AK nalezena ({len(ac_text)} znaku)")

    repo_slug = get_e2e_repo_slug(project_key)
    print(f"[BB] E2E repo: {repo_slug}")

    e2e_config = await get_e2e_config(repo_slug)
    folder, dev_url = resolve_component(e2e_config, jira_components)
    print(f"[PW] Slozka: {folder or 'root'} | DEV URL: {dev_url}")

    # Ziskej linked PR a stahni zmenene soubory
    source_files = []
    if issue_id:
        pr_result = await get_linked_pr(issue_id)
        if pr_result:
            pr_repo_slug, pr_id = pr_result
            source_files = await get_pr_diff_files(pr_repo_slug, pr_id)
            print(f"[PW] Stazeno {len(source_files)} zdrojovych souboru z PR #{pr_id}")
        else:
            print(f"[PW] Zadne linked PR — generuji bez source kontextu")

    # Detekuj stack verze
    stack_versions = {}
    if issue_id and source_files:
        pr_result2 = await get_linked_pr(issue_id)
        if pr_result2:
            stack_versions = await detect_stack_versions(pr_result2[0], pr_result2[1])

    if DEBUG_RUN:
        print(f"[DEBUG_RUN] repo: {repo_slug} | folder: {folder} | url: {dev_url} | source files: {len(source_files)} | stack: {stack_versions}")
        return JSONResponse({"status": "debug_run", "repo": repo_slug, "folder": folder, "dev_url": dev_url, "source_files": len(source_files), "stack": stack_versions})

    print(f"[PW] Generuji Playwright testy pro {issue_key} | source files: {len(source_files)} | stack: {stack_versions}...")
    test_code = await generate_playwright_tests(issue_key, summary, ac_text, dev_url, source_files, stack_versions)
    print(f"[PW] Vygenerovano {len(test_code)} znaku kodu")

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
