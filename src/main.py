import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request

from .config import load
from .github_events import parse_pull_request
from .github_signature import verify
from .handler import handle_plane_event, handle_pull_request
from .plane_client import PlaneClient
from .plane_events import parse_plane_event
from .plane_signature import verify as verify_plane
from .resolver import Resolver


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("pylon")


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required env var {name} is not set")
    return value


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = load(os.environ.get("CONFIG_PATH", "config.yaml"))
    plane = PlaneClient(
        base_url=cfg.plane_base_url,
        workspace_slug=cfg.plane_workspace,
        api_key=_required_env("PLANE_API_KEY"),
    )
    app.state.cfg = cfg
    app.state.plane = plane
    app.state.resolver = Resolver(plane)
    app.state.webhook_secret = _required_env("GITHUB_WEBHOOK_SECRET")
    # PLANE_WEBHOOK_SECRET is optional — when unset, the Plane
    # webhook route accepts unsigned deliveries with a startup
    # warning. Plane CE doesn't mandate webhook signing.
    app.state.plane_webhook_secret = os.environ.get("PLANE_WEBHOOK_SECRET", "")
    if not app.state.plane_webhook_secret:
        logger.warning(
            "PLANE_WEBHOOK_SECRET is unset — Plane webhook route will "
            "accept unsigned deliveries. Set the env var for signature "
            "verification."
        )
    # FORGEJO_WEBHOOK_SECRET is optional too — when unset, the Forgejo
    # webhook route accepts unsigned deliveries with a startup warning,
    # so pylon still boots before the forge is wired up.
    app.state.forgejo_webhook_secret = os.environ.get("FORGEJO_WEBHOOK_SECRET", "")
    if not app.state.forgejo_webhook_secret:
        logger.warning(
            "FORGEJO_WEBHOOK_SECRET is unset — Forgejo webhook route will "
            "accept unsigned deliveries. Set the env var for signature "
            "verification."
        )
    app.state.last_webhook = {"at": None, "event": None, "status": None}
    app.state.webhook_count = 0
    logger.info(
        "pylon started workspace=%s base=%s",
        cfg.plane_workspace,
        cfg.plane_base_url,
    )
    try:
        yield
    finally:
        await plane.aclose()


app = FastAPI(lifespan=lifespan, title="pylon", version="0.2.0")


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, object]:
    return {
        "status": "ok",
        "webhook_count": request.app.state.webhook_count,
        "last_webhook": request.app.state.last_webhook,
    }


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, object]:
    body = await request.body()
    request.app.state.webhook_count += 1
    request.app.state.last_webhook = {
        "at": time.time(),
        "event": x_github_event,
        "delivery": x_github_delivery,
        "status": "received",
    }
    logger.info(
        "webhook received event=%s delivery=%s body_bytes=%d",
        x_github_event or "<none>",
        x_github_delivery or "<none>",
        len(body),
    )

    if not verify(request.app.state.webhook_secret, body, x_hub_signature_256):
        request.app.state.last_webhook["status"] = "bad_signature"
        logger.warning(
            "rejected webhook: bad signature event=%s delivery=%s",
            x_github_event,
            x_github_delivery,
        )
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event == "ping":
        request.app.state.last_webhook["status"] = "pong"
        logger.info("ping ack delivery=%s", x_github_delivery)
        return {"status": "pong"}
    if x_github_event != "pull_request":
        request.app.state.last_webhook["status"] = "ignored_event_type"
        return {"status": "ignored", "event": x_github_event}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        request.app.state.last_webhook["status"] = "bad_json"
        raise HTTPException(status_code=400, detail=f"invalid json: {e}")

    event = parse_pull_request(payload)
    logger.info(
        "pull_request action=%s pr=#%s refs=%s repo=%s",
        event.action,
        event.number,
        [str(r) for r in event.refs],
        (payload.get("repository") or {}).get("full_name") or "<unknown>",
    )
    try:
        await handle_pull_request(
            event,
            request.app.state.cfg,
            request.app.state.plane,
            request.app.state.resolver,
        )
        status = "processed"
    except Exception:
        logger.exception("handler raised on pr=#%s delivery=%s", event.number, x_github_delivery)
        status = "handler_error"

    request.app.state.last_webhook["status"] = status
    logger.info(
        "webhook done pr=#%s status=%s refs=%s",
        event.number,
        status,
        [str(r) for r in event.refs],
    )

    if status == "handler_error":
        # Return a non-2xx so GitHub's built-in webhook delivery retry
        # re-runs this event. Previously the handler swallowed every
        # failure and still answered 200, so a transient Plane-API blip
        # (429/timeout during a batch-merge burst) silently dropped the
        # ticket transition forever — GitHub only retries on non-2xx /
        # timeout. The handler's mutations are idempotent on replay
        # (state PATCH is set-to-target, links dedup), so a retry is
        # safe. 503 signals "transient, try again".
        raise HTTPException(
            status_code=503,
            detail={
                "status": status,
                "action": event.action,
                "pr": event.number,
                "refs": [str(r) for r in event.refs],
            },
        )

    return {
        "status": status,
        "action": event.action,
        "pr": event.number,
        "refs": [str(r) for r in event.refs],
    }


# ── Plane webhook ──────────────────────────────────────────────────
#
# Plane CE fires webhooks for every object change in the workspace.
# pylon only acts on `issue.updated` events, where the corresponding
# work-item state may have moved — that's the trigger for the module
# reconciler. Other Plane events ack 200 and log+skip.
#
# Why this route exists alongside /webhook/github:
# the GitHub route only catches PR-driven state changes; the Plane
# route catches everything else (UI edits, MCP/REST calls, future
# automations). Both feed the same shared _reconcile_module logic in
# handler.py.


# ── Forgejo webhook ─────────────────────────────────────────────────
#
# Forgejo (git.swagner.tech) fires the same pull_request events as GitHub,
# with a GitHub-compatible payload and HMAC (X-Hub-Signature-256). We reuse
# the GitHub parser (parse_pull_request) and the shared PR→Plane handler
# verbatim — only the event header (X-Forgejo-Event, falling back to the
# Gitea-compatible X-Gitea-Event) and the secret differ. The leading-[QF-NN]
# title-prefix parser is identical. See FORGEJO_MIGRATION_PLAN.md.


@app.post("/webhook/forgejo")
async def forgejo_webhook(
    request: Request,
    x_forgejo_event: str = Header(default=""),
    x_gitea_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, object]:
    body = await request.body()
    event_type = x_forgejo_event or x_gitea_event
    request.app.state.webhook_count += 1
    request.app.state.last_webhook = {
        "at": time.time(),
        "event": event_type,
        "source": "forgejo",
        "status": "received",
    }
    logger.info(
        "forgejo webhook received event=%s body_bytes=%d",
        event_type or "<none>",
        len(body),
    )

    secret = request.app.state.forgejo_webhook_secret
    if secret and not verify(secret, body, x_hub_signature_256):
        request.app.state.last_webhook["status"] = "bad_signature"
        logger.warning(
            "rejected forgejo webhook: bad signature event=%s", event_type
        )
        raise HTTPException(status_code=401, detail="invalid signature")

    if event_type == "ping":
        request.app.state.last_webhook["status"] = "pong"
        logger.info("forgejo ping ack")
        return {"status": "pong"}
    if event_type != "pull_request":
        request.app.state.last_webhook["status"] = "ignored_event_type"
        return {"status": "ignored", "event": event_type}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        request.app.state.last_webhook["status"] = "bad_json"
        raise HTTPException(status_code=400, detail=f"invalid json: {e}")

    event = parse_pull_request(payload)
    logger.info(
        "forgejo pull_request action=%s pr=#%s refs=%s repo=%s",
        event.action,
        event.number,
        [str(r) for r in event.refs],
        (payload.get("repository") or {}).get("full_name") or "<unknown>",
    )
    try:
        await handle_pull_request(
            event,
            request.app.state.cfg,
            request.app.state.plane,
            request.app.state.resolver,
        )
        status = "processed"
    except Exception:
        logger.exception("forgejo handler raised on pr=#%s", event.number)
        status = "handler_error"

    request.app.state.last_webhook["status"] = status
    logger.info(
        "forgejo webhook done pr=#%s status=%s refs=%s",
        event.number,
        status,
        [str(r) for r in event.refs],
    )

    if status == "handler_error":
        # Non-2xx so Forgejo's webhook delivery retry re-runs the event;
        # handler mutations are idempotent on replay (same as the GitHub route).
        raise HTTPException(
            status_code=503,
            detail={
                "status": status,
                "action": event.action,
                "pr": event.number,
                "refs": [str(r) for r in event.refs],
            },
        )

    return {
        "status": status,
        "action": event.action,
        "pr": event.number,
        "refs": [str(r) for r in event.refs],
    }


@app.post("/webhook/plane")
async def plane_webhook(
    request: Request,
    x_plane_event: str = Header(default=""),
    x_plane_delivery: str = Header(default=""),
    x_plane_signature: str | None = Header(default=None),
) -> dict[str, object]:
    body = await request.body()
    request.app.state.webhook_count += 1
    request.app.state.last_webhook = {
        "at": time.time(),
        "event": x_plane_event,
        "delivery": x_plane_delivery,
        "source": "plane",
        "status": "received",
    }
    logger.info(
        "plane webhook received event=%s delivery=%s body_bytes=%d",
        x_plane_event or "<none>",
        x_plane_delivery or "<none>",
        len(body),
    )

    if not verify_plane(
        request.app.state.plane_webhook_secret, body, x_plane_signature
    ):
        request.app.state.last_webhook["status"] = "bad_signature"
        # Diagnostic logging on bad signature. No secrets logged — only
        # body fingerprint + HMAC values, which are themselves keyed
        # one-way digests. The expected-from-reserialized variant tests
        # the hypothesis that Plane signs json.dumps(payload) while
        # `requests.post(json=payload)` sends a slightly different
        # byte sequence (e.g. when simplejson is installed and silently
        # replaces stdlib json in the requests body path).
        secret = request.app.state.plane_webhook_secret
        presented = (x_plane_signature or "").strip().lower()
        if presented.startswith("sha256="):
            presented = presented[len("sha256="):]
        expected_raw = (
            hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if secret
            else "<no-secret>"
        )
        expected_reserialized = "<n/a>"
        try:
            parsed = json.loads(body)
            reserialized = json.dumps(parsed).encode("utf-8")
            if secret:
                expected_reserialized = hmac.new(
                    secret.encode(), reserialized, hashlib.sha256
                ).hexdigest()
        except (json.JSONDecodeError, TypeError):
            pass
        logger.warning(
            "rejected plane webhook: bad signature event=%s delivery=%s "
            "body_sha=%s body_len=%d presented=%s expected_raw=%s expected_reserialized=%s",
            x_plane_event,
            x_plane_delivery,
            hashlib.sha256(body).hexdigest(),
            len(body),
            presented or "<empty>",
            expected_raw,
            expected_reserialized,
        )
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        request.app.state.last_webhook["status"] = "bad_json"
        raise HTTPException(status_code=400, detail=f"invalid json: {e}")
    if not isinstance(payload, dict):
        request.app.state.last_webhook["status"] = "bad_payload"
        raise HTTPException(status_code=400, detail="payload is not an object")

    event = parse_plane_event(payload)
    logger.info(
        "plane event parsed event=%s action=%s work_item=%s modules=%d",
        event.raw_event,
        event.raw_action,
        event.work_item_id or "<unknown>",
        len(event.module_ids),
    )

    try:
        await handle_plane_event(
            event,
            request.app.state.cfg,
            request.app.state.plane,
            request.app.state.resolver,
        )
        status = "processed" if event.action != "ignored" else "ignored"
    except Exception:
        logger.exception(
            "plane handler raised event=%s delivery=%s work_item=%s",
            event.raw_event,
            x_plane_delivery,
            event.work_item_id,
        )
        status = "handler_error"

    request.app.state.last_webhook["status"] = status
    return {
        "status": status,
        "event": event.raw_event,
        "action": event.raw_action,
        "work_item": event.work_item_id,
        "module_count": len(event.module_ids),
    }
