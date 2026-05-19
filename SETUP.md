# SETUP.md — deploying pylon end-to-end

This is the operator-side companion to the README. The README explains *what*
pylon does; this file explains *what you have to configure* on Plane and GitHub
to make it work.

## Scope

One pylon instance handles **one Plane workspace** and **any number of GitHub
repositories** that push pull-request events at it. Inside the workspace,
projects are auto-discovered: adding a new project to Plane requires zero
pylon redeploy, and the cache is rebuilt lazily on first reference
([src/resolver.py:69-90](src/resolver.py#L69-L90)).

There is no per-project configuration. There is no allowlist. Any project
pylon sees in the workspace is potentially in scope — refs whose prefix
doesn't match any project are logged and skipped.

## Plane setup

### 1. Create or pick a service account

pylon authenticates to Plane as a single workspace member. That member needs
**write access on work items in every project pylon handles** (state
transitions, link creation, comment posting). A dedicated bot account is
recommended so the audit trail is unambiguous, but any sufficiently
privileged workspace member works.

### 2. Generate a Personal Access Token

Sign in as that member and go to `{plane_base_url}/profile/api-tokens`. Create
a token. Put it in your `.env` as `PLANE_API_KEY`:

```bash
PLANE_API_KEY=plane_api_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

pylon passes this token in the `X-API-Key` header on every Plane request.

### 3. Ensure each project has the configured state names

pylon's `config.yaml` declares the state name each PR action should
transition the work item to. The defaults:

| PR action | State name |
|---|---|
| `opened_draft` / `converted_to_draft` | `In Progress` |
| `opened_ready` / `ready_for_review` | `In Review` |
| `merged` | `Done` |
| `closed_unmerged` (when previously in `In Review`) | `In Progress` |

Matching is case-insensitive. Any project pylon handles must have these state
names — or rename them in `config.yaml`'s `state_machine:` block before
deploying. **A project missing a configured state name has *that one
transition* logged and skipped, not failed**; the webhook still returns 200
to GitHub. This is intentional: a partial-coverage state machine is better
than a 500 that GitHub then retries.

### 4. (Strongly recommended) Subscribe to Plane's webhooks

In the workspace settings → Webhooks, add a webhook:

- **URL:** `https://your-pylon-host/webhook/plane`
- **Events:** issue (specifically `issue.created` / `issue.updated`)
- **Secret:** generate with `openssl rand -hex 32`; store the same value in
  your `.env` as `PLANE_WEBHOOK_SECRET`.

pylon verifies HMAC-SHA256 over the raw body against the `x-plane-signature`
header.

If you skip this step, pylon still works — but it only reacts to
GitHub-driven state changes. Manual Plane UI edits, MCP / REST API calls, and
other automations won't trigger module reconciliation. If `PLANE_WEBHOOK_SECRET`
is unset, pylon logs a startup warning and accepts unsigned deliveries — safe
in a private network, risky on the public internet.

## GitHub setup

GitHub org-level webhooks can't target `pull_request` events, so pylon's
GitHub webhook is **per-repository**. For each repo pylon should watch:

1. Repo → **Settings** → **Webhooks** → **Add webhook**.
2. **Payload URL:** `https://your-pylon-host/webhook/github`
3. **Content type:** `application/json`
4. **Secret:** generate with `openssl rand -hex 32`; store the same value
   in your `.env` as `GITHUB_WEBHOOK_SECRET`. All repos that point at the
   same pylon instance must use the same secret.
5. **Events:** **Let me select individual events** → check only
   **Pull requests**. pylon ignores everything else
   ([src/main.py:118-120](src/main.py#L118-L120)).

The actions pylon acts on are `opened`, `reopened`, `ready_for_review`,
`converted_to_draft`, and `closed` — all part of the `pull_request` event
type.

## Network

```
GitHub  ──HTTPS──▶  reverse proxy  ──HTTP──▶  pylon (port 8000)
                                                   │
                                            pylon  ─┴──HTTPS──▶  Plane REST API
                                                   ▲
Plane (webhooks)  ──HTTPS──▶  reverse proxy  ──HTTP──┘
```

- GitHub enforces HTTPS for webhook deliveries — TLS-terminating reverse
  proxy is mandatory.
- pylon makes outbound calls to your Plane base URL (`plane_base_url` in
  `config.yaml`).
- Plane → pylon for the optional second webhook flows through the same
  reverse proxy.

pylon is stateless — no database. Its only in-memory state is a cache of
project IDs and state UUIDs from Plane (`Resolver`), populated lazily and
discarded on process restart.

## When to restart pylon

Restart pylon when:

- You **rename a state** in a project, or
- You **rename or recreate a project** (the project ID changes).

You do **not** need to restart for:

- New projects added to the workspace (lazy-loaded on first reference).
- New tickets, new modules, new state transitions — those are read fresh
  from Plane on every webhook.

Restart with `docker compose restart pylon` (or equivalent). It takes a few
seconds and the next webhook re-populates the cache.

## First-deploy smoke test

After pylon is running and both webhooks are configured:

1. Open a PR titled `feat: smoke test (XX-NN)` against a real Plane ticket
   currently in your "todo" state. Verify the ticket moves to **In
   Review** and the PR URL appears as a link in Plane.
2. Merge the PR. Verify the ticket moves to **Done**.
3. Open another PR `[XX-MM]`, close without merging. Verify the ticket
   moves back to **In Progress** (if it was in `In Review`) and a comment
   with the PR URL is posted.

If a step fails, check `docker compose logs pylon` and the GitHub webhook
deliveries page (repo → Settings → Webhooks → Recent Deliveries) for the
request/response.

pylon also exposes a health endpoint: `curl https://your-pylon-host/healthz`
returns `{"status": "ok", ...}` and includes a `last_webhook` field useful
for confirming deliveries are arriving.

## Footguns

- **A PR title with an unknown project prefix** (`WEB-25:` when no `WEB`
  project exists in Plane) is logged and skipped; the webhook still returns
  200. This tolerates false positives like `PR-25` and `RFC-8174`. Cost is
  bounded log noise.

- **Plane is down when a webhook arrives** — pylon logs the error and returns
  200 to GitHub. **GitHub will not retry** (pylon has already ack'd). Recovery
  is manual: re-deliver from GitHub's webhook deliveries page, or transition
  the ticket in Plane by hand.

- **The module-enumeration fallback is O(M) API calls** per state change for
  projects whose Plane build omits `module_ids` from webhook payloads. Fine
  for human-paced editing on workspaces with low double-digit module counts;
  consider rate-limit headroom on Plane if you're at hundreds.

- **No `PLANE_WEBHOOK_SECRET` is set** — pylon warns at startup and accepts
  unsigned Plane webhooks. Always set the secret on internet-exposed
  deployments.

- **Per-ref errors don't block other refs.** A bundled PR titled
  `QF-12 QF-13: bundle` whose `QF-13` lookup fails still processes `QF-12`
  ([src/handler.py:76-81](src/handler.py#L76-L81)).
