# pylon

A minimal, self-hosted webhook bridge that keeps a [Plane](https://plane.so) Community Edition workspace consistent with external events. Plane's first-party integrations are gated to the Commercial Edition; pylon is a small replacement that drives:

- **Work-item state transitions from GitHub PRs** (the original use case — Plane CE has no GitHub integration).
- **Module-status reconciliation from any state change** — GitHub-PR-driven *or* manual edits in the Plane UI *or* MCP / REST API calls *or* future automations. Subscribes to Plane's own webhooks for that second source.

For end-to-end deployment instructions — generating the Plane PAT, configuring webhooks on both sides, required state names per project — see [SETUP.md](SETUP.md).

## What it does

When a GitHub PR's title **begins** with a Plane work-item identifier — either bracketed (`[QF-12]`, the convention Plane Commercial uses) or bare (`QF-12`, convenient in natural-language PR titles) — pylon updates the corresponding Plane ticket via Plane's REST API:

| GitHub event | Condition | Plane action |
|---|---|---|
| `pull_request.opened` / `reopened` | draft = true | state → **In Progress**, attach link |
| `pull_request.opened` / `reopened` | draft = false | state → **In Review**, attach link |
| `pull_request.ready_for_review` | — | state → **In Review**, attach link |
| `pull_request.converted_to_draft` | — | state → **In Progress** |
| `pull_request.closed` | merged = true | state → **Testing** |
| `pull_request.closed` | merged = false, current state = In Review | state → **In Progress**, post comment |
| `pull_request.closed` | merged = false, current state ≠ In Review | post comment only |

The state names are configurable in [config.yaml](config.yaml)'s `state_machine:` block. The names listed above are the defaults. Each Plane project pylon handles must have these state names — pylon logs and skips any transition whose target name doesn't exist.

## Module-state reconciliation

After each work-item state transition lands, pylon inspects the modules the ticket belongs to and may update each module's `status` field:

| Module status before | Item state-group conditions | Module status after |
|---|---|---|
| `planned` / `backlog` | at least one item is in the `started` group | `in-progress` |
| anything except `paused` / `cancelled` | every item is in the `completed` or `cancelled` group (and ≥ 1 item) | `completed` |
| `completed` | at least one item is **not** in the `completed` / `cancelled` group (reopened ticket, or a freshly-added one) | `in-progress` |
| `paused` / `cancelled` | — | left alone (operator-managed terminal states) |

State groups come from Plane's standard taxonomy: `backlog`, `unstarted`, `started`, `completed`, `cancelled`. Pylon reads each project's `/states/` list and uses the `group` field on each state — so the rule fires regardless of state naming (e.g. "In Review", "Testing", and "In Progress" all count as `started`).

An all-cancelled module is treated as `completed` — the rule is "no more work to do", not "everything succeeded". Empty modules are never auto-transitioned. `completed` is **auto-reversible**: when a ticket is reopened or a non-terminal ticket is added, the module flips back to `in-progress` so dashboards stay honest. `paused` and `cancelled` are the only operator-managed terminal states.

Disable the feature globally by setting `modules.enabled: false` in [config.yaml](config.yaml).

## Trigger sources for module reconciliation

The same module-reconciliation logic fires from two webhook endpoints, so module status stays consistent regardless of how a ticket's state changed:

| Endpoint | Triggered by | What fires |
|---|---|---|
| `POST /webhook/github` | `pull_request` events (open, ready, closed, …) | Apply the PR state machine → ticket state change → reconcile the ticket's modules. |
| `POST /webhook/plane` | Plane's own `issue.created` / `issue.updated` events | Reconcile the ticket's modules. (pylon doesn't change ticket state from this path — Plane is the source of truth for what already happened.) |

The Plane webhook catches state changes pylon didn't drive: edits made in the Plane UI, MCP calls, direct REST calls, future automations. Both endpoints share the same `_reconcile_module` core, so the behaviour table above applies regardless of trigger source.

### Feedback-loop safety

pylon's GitHub handler changes a ticket state → Plane fires `issue.updated` → pylon's Plane handler receives it. The Plane handler asks the reconciler "does the module need a status update?" and the reconciler answers "no — current_status matches the desired status" → no-op. No infinite loop, no thrash.

### Configuring Plane's webhook

In the Plane UI, point a webhook at `https://your-pylon-host/webhook/plane` and subscribe to **issue** events. Optionally set a secret and put the same value in `PLANE_WEBHOOK_SECRET` on the pylon container; pylon verifies HMAC-SHA256 over the raw body against the `x-plane-signature` header. If the env var is unset, pylon logs a startup warning and accepts unsigned deliveries.

## How refs are extracted

pylon scans **only the leading prefix of the PR title** — body text, mid-title refs, and trailing parentheticals are deliberately ignored. This rule keeps "future work in QF-91 / QF-92" mentions in PR bodies from auto-attaching unrelated tickets.

The leading prefix accepts one or more refs, optionally bracket-wrapped, whitespace- or comma-separated, terminated by a colon, whitespace, or end-of-title:

| Title | Refs picked up |
|---|---|
| `QF-12: add foo` | `QF-12` |
| `[QF-12] add foo` | `QF-12` |
| `[QF-12]: add foo` | `QF-12` |
| `QF-12 QF-13: bundle` | `QF-12`, `QF-13` |
| `[QF-12 QF-13] bundle` | `QF-12`, `QF-13` |
| `QF-12, QF-13: bundle` | `QF-12`, `QF-13` |
| `feat: add foo (QF-12)` | *(none — mid-title)* |
| `Revert QF-84 changes` | *(none — not at start)* |
| `subject` with `Closes QF-12` in body | *(none — body ignored)* |

Duplicate refs in the prefix dedupe to a single update. False-positive prefixes (`PR-25` for a GitHub PR reference, `RFC-8174`, etc.) are tolerated: pylon resolves the project prefix against the Plane workspace at lookup time and logs-and-skips refs whose prefix isn't a known project. Cost is bounded log noise, not bad state transitions.

Out of scope: GitHub Issues sync, bidirectional comment sync, code-review events.

## Plane API quirks pylon works around

Plane CE's REST API and webhook payloads have a handful of sharp edges that pylon papers over. These are worth knowing if you're forking pylon, debugging a deployment, or considering similar integrations.

- **No "modules for this work item" inverse lookup.** Plane CE's external `IssueSerializer` doesn't expose `module_ids` and `GET /issues/{id}` doesn't either — the link only lives in the `IssueModule` join table, which only the internal serializer reads. When a Plane webhook arrives without `module_ids` in the payload, pylon enumerates every module in the project and probes each one's work-item list to find membership ([src/handler.py:340-393](src/handler.py#L340-L393), [src/plane_client.py:58](src/plane_client.py#L58)). **Cost:** O(M) API calls per state change, where M is module count in that project — fine for human-paced edits, costly for bots. When Plane *does* ship `module_ids` in the payload, pylon uses it directly and skips the fallback ([src/handler.py:318-323](src/handler.py#L318-L323)).

- **Pagination has no truthy end signal in `next_cursor`.** Plane CE's paginated envelopes return a non-empty `next_cursor` even on the final page, so naive clients spin forever. Pylon stops when `next_page_results` is `False` ([src/plane_client.py:219-248](src/plane_client.py#L219-L248)). The bug surfaced after the module-enumeration fallback hit Plane's HTTP 429 rate limit on a 16-module project.

- **Webhook signature mismatches can be silent.** Some Plane builds re-serialize the JSON body before signing, producing an HMAC that doesn't match the raw bytes pylon receives. On signature mismatch pylon logs the body fingerprint, the presented signature, the HMAC over raw bytes, **and** the HMAC over `json.dumps(payload)` to make the cause obvious ([src/main.py:203-240](src/main.py#L203-L240)). No secret material is logged.

- **Required state names are project-local and matched leniently.** Plane lets each project define its own state names; pylon matches `config.yaml`'s `state_machine:` block against them case-insensitively. A project missing a configured state name has *that one transition* logged and skipped — pylon keeps operating on other actions and projects rather than hard-failing ([src/resolver.py:49](src/resolver.py#L49), [src/handler.py:131-143](src/handler.py#L131-L143)).

## Architecture

- Receives GitHub webhooks (`pull_request` events) at `POST /webhook/github`.
- Verifies the `X-Hub-Signature-256` HMAC against `GITHUB_WEBHOOK_SECRET`.
- Parses the payload, extracts `[XX-NN]` references, calls Plane's REST API authenticated with `PLANE_API_KEY`.
- Stateless. No database. Configuration in `config.yaml` + env.

## Configuration

Two layers:

1. [config.yaml](config.yaml) (committed) — Plane base URL, workspace slug, and the state-machine policy (which Plane state name each PR action transitions to).
2. `.env` (host-only, see [.env.example](.env.example)) — `PLANE_API_KEY`, `GITHUB_WEBHOOK_SECRET`, `CONFIG_PATH`, `LOG_LEVEL`.

There's no per-project configuration. Pylon discovers projects and their state UUIDs from Plane at runtime and caches them for the lifetime of the process. To add a new project to pylon: just create it in Plane with the state names listed above. To rename or reorganize states in an existing project: restart pylon (`docker compose restart pylon`) so the cache reloads.

## Local development

```bash
uv sync
uv run pytest
uv run uvicorn src.main:app --reload --port 8000
```

Health check: `curl localhost:8000/healthz`.

## Deployment

pylon is built to run as a Docker container behind a TLS-terminating reverse proxy (Traefik, Caddy, nginx, etc.). The shipped [Dockerfile](Dockerfile) produces a `uvicorn`-served image exposing port 8000.

A minimal Compose snippet:

```yaml
services:
  pylon:
    build: .
    restart: unless-stopped
    env_file: .env
    # Behind a reverse proxy that handles TLS — pylon speaks plain HTTP
    # internally on port 8000.
    expose:
      - "8000"
```

GitHub requires HTTPS for webhook deliveries, so a TLS reverse proxy is mandatory in any internet-reachable deployment. Point the proxy's public hostname at the pylon container's port 8000.

For the full operator walkthrough — generating the Plane PAT, configuring webhooks on both GitHub and Plane, required state names per project, and a post-deploy smoke test — see [SETUP.md](SETUP.md).

## License

MIT — see [LICENSE](LICENSE).
