# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities **privately** rather than opening a public issue.

The preferred channel is GitHub's private security advisory feature:

1. Go to https://github.com/abwagner/pylon/security/advisories/new
2. Describe the vulnerability, reproduction steps, and impact.
3. We'll triage as quickly as we can.

If you cannot use GitHub advisories, contact the maintainers directly through any contact info listed on the repository profile.

## Supported Versions

pylon is early-stage (0.x). Only the latest `main` branch receives security fixes. There is no LTS or backporting policy yet.

## Scope

In scope:

- The pylon source under `src/`, particularly:
  - Webhook signature verification ([src/github_signature.py](src/github_signature.py), [src/plane_signature.py](src/plane_signature.py)).
  - PR-title parsing and ref extraction ([src/github_events.py](src/github_events.py)).
  - The Plane REST client and its authentication path ([src/plane_client.py](src/plane_client.py)).
- The shipped [Dockerfile](Dockerfile) and [config.yaml](config.yaml).

Out of scope:

- Third-party dependencies — please report upstream (FastAPI, httpx, pydantic, Plane itself).
- Issues that require the attacker to already control `PLANE_API_KEY`, the webhook secrets, or the host filesystem.
- Denial-of-service via unbounded webhook deliveries — pylon is designed to be reverse-proxied; rate limiting and auth gating are the operator's responsibility.

## Threat model notes

pylon's webhook endpoints (`/webhook/github`, `/webhook/plane`) both verify HMAC-SHA256 against a shared secret. Vulnerabilities in that path (timing attacks, signature bypass, body-tampering windows) are the highest-priority class. The HMAC implementation deliberately compares with `hmac.compare_digest` to avoid timing leaks.

If `PLANE_WEBHOOK_SECRET` is unset, pylon accepts unsigned Plane webhooks and logs a startup warning — this is documented as a known footgun in [SETUP.md](SETUP.md), not a vulnerability.
