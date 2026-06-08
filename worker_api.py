"""
Worker API node (MODE=worker).
==============================

A headless FastAPI service that the master reaches over an SSH tunnel. It
executes login / send jobs by calling the EXISTING, UNCHANGED functions in
`rubika_client.py`. There is no Telegram panel here — this process only takes
orders from the master.

Endpoints (all require `Authorization: Bearer <WORKER_API_TOKEN>`):
  GET  /health                 -> {file_ok, status_code}     (Rubika route check)
  POST /login/start            -> {ok, needs_password, needs_code, status}
  POST /login/password         -> {ok, needs_code}
  POST /login/code             -> {ok, name, guid, contacts, groups, with_chat}
  POST /send/start             -> {ok, job_id, total, marker_found}
  GET  /send/status/{job_id}   -> {ok, fail, total, done, stopped, reason}
  POST /send/stop/{job_id}     -> {stopped: true}

It binds to loopback only; the master maps a local port to it via SSH, so the
API is never exposed to the public internet.
"""
import asyncio
import random
import uuid

import config
import rubika_client as rb

# In worker mode we only need FastAPI + uvicorn + httpx; import lazily so this
# file can still be byte-compiled on the master without those installed.
try:
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel
    _HAVE_FASTAPI = True
except ImportError:  # pragma: no cover
    FastAPI = object  # type: ignore
    Header = None  # type: ignore
    HTTPException = Exception  # type: ignore
    BaseModel = object  # type: ignore
    _HAVE_FASTAPI = False


# --------------------------------------------------------------------------- #
# In-memory state (lives only inside this worker process).
# --------------------------------------------------------------------------- #
_login_ctx: dict = {}   # phone -> rubpy login context dict
_jobs: dict = {}        # job_id -> job state dict
_automations: dict = {}  # phone -> automation state dict


def _build_app():
    app = FastAPI(title="V2Rubby Worker", docs_url=None, redoc_url=None)

    def _auth(authorization: str):
        expected = config.WORKER_API_TOKEN
        if not expected:
            raise HTTPException(status_code=500, detail="worker token not configured")
        if not authorization or authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="unauthorized")

    # ----- request models -----
    class StartLogin(BaseModel):
        phone: str
        pass_key: str = None

    class CodeIn(BaseModel):
        phone: str
        code: str

    class PasswordIn(BaseModel):
        phone: str
        password: str

    class SendIn(BaseModel):
        phone: str
        marker: str
        delay: float = 1.0
        max_errors: int = 3
        send_timeout: int = 60
        resume_wait: int = 300
        max_retries: int = 2

    class AutomationIn(BaseModel):
        phone: str
        texts: list = []
        interval: int = 30

    class PrepareIn(BaseModel):
        phone: str
        marker: str

    class ChannelCreateIn(BaseModel):
        phone: str
        marker: str
        title: str

    class ChannelAddIn(BaseModel):
        phone: str
        channel_guid: str
        target: int = 300
        batch: int = 80
        delay: float = 2.0

    # ----- ping (NO token; just proves the API process is alive) -----
    @app.get("/ping")
    async def ping():
        return {"ok": True, "service": "v2rubby-worker"}

    # ----- health -----
    @app.get("/health")
    async def health(authorization: str = Header(None)):
        _auth(authorization)
        import httpx
        code = None
        file_ok = False
        try:
            async with httpx.AsyncClient(timeout=config.HEALTH_TIMEOUT) as c:
                r = await c.get(config.HEALTH_URL)
                code = r.status_code
                file_ok = code in (200, 404)
        except Exception:
            file_ok = False
        return {"file_ok": file_ok, "status_code": code}

    # ----- login relay -----
    @app.post("/login/start")
    async def login_start(body: StartLogin, authorization: str = Header(None)):
        _auth(authorization)
        ctx = await rb.start_login(body.phone, pass_key=body.pass_key)
        _login_ctx[rb.normalize_phone(body.phone)] = ctx
        status = str(ctx.get("status") or "").upper()
        needs_password = "PASS" in status
        needs_code = (not needs_password) and bool(ctx.get("phone_code_hash"))
        return {"ok": True, "status": status,
                "needs_password": needs_password, "needs_code": needs_code}

    @app.post("/login/password")
    async def login_password(body: PasswordIn, authorization: str = Header(None)):
        _auth(authorization)
        ctx = await rb.start_login(body.phone, pass_key=body.password)
        _login_ctx[rb.normalize_phone(body.phone)] = ctx
        return {"ok": True, "needs_code": bool(ctx.get("phone_code_hash"))}

    @app.post("/login/code")
    async def login_code(body: CodeIn, authorization: str = Header(None)):
        _auth(authorization)
        key = rb.normalize_phone(body.phone)
        ctx = _login_ctx.get(key)
        if not ctx:
            raise HTTPException(status_code=400, detail="no login in progress")
        code = "".join(ch for ch in body.code if ch.isdigit())
        await rb.finish_login(ctx, code)
        client = ctx["client"]
        try:
            me = await client.get_me()
            guid = rb._guid_of(me) or "-"
            name = rb._name_of(me)
            _ordered, stats = await rb.get_ordered_recipients(client)
            return {"ok": True, "name": name, "guid": str(guid),
                    "contacts": stats["contacts"], "groups": stats["groups"],
                    "with_chat": stats["with_chat"], "phone": ctx["phone"]}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            _login_ctx.pop(key, None)

    # ----- send -----
    @app.post("/prepare")
    async def prepare(body: PrepareIn, authorization: str = Header(None)):
        _auth(authorization)
        client = rb.open_client(body.phone)
        try:
            await rb.connect_ready(client)
            saved_guid, mid = await rb.find_marked_message(client, body.marker)
            if not mid:
                return {"ok": True, "marker_found": False, "total": 0}
            ordered, _stats = await rb.get_ordered_recipients(client)
            return {"ok": True, "marker_found": True, "total": len(ordered)}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    # ----- channel send mode -----
    @app.post("/channel/create")
    async def channel_create(body: ChannelCreateIn, authorization: str = Header(None)):
        _auth(authorization)
        client = rb.open_client(body.phone)
        try:
            await rb.connect_ready(client)
            saved_guid, mid = await rb.find_marked_message(client, body.marker)
            channel_guid = await rb.create_channel(client, body.title)
            forwarded = False
            if mid:
                try:
                    await rb.forward_message(client, saved_guid, channel_guid, mid)
                    forwarded = True
                except Exception:
                    forwarded = False
            return {"ok": True, "channel_guid": channel_guid,
                    "marker_found": bool(mid), "forwarded": forwarded}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @app.post("/channel/add")
    async def channel_add(body: ChannelAddIn, authorization: str = Header(None)):
        _auth(authorization)
        client = rb.open_client(body.phone)
        try:
            await rb.connect_ready(client)
            added = await rb.seed_channel_with_contacts(
                client, body.channel_guid, target=body.target,
                batch=body.batch, delay=body.delay)
            return {"ok": True, "added": added}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @app.post("/send/start")
    async def send_start(body: SendIn, authorization: str = Header(None)):
        _auth(authorization)
        client = rb.open_client(body.phone)
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, body.marker)
        if not mid:
            try:
                await client.disconnect()
            except Exception:
                pass
            return {"ok": False, "marker_found": False, "total": 0}
        ordered, _stats = await rb.get_ordered_recipients(client)
        recipients = [r["guid"] for r in ordered]

        job_id = uuid.uuid4().hex[:12]
        job = {"phone": body.phone, "total": len(recipients), "ok": 0, "fail": 0,
               "done": False, "stopped": False, "reason": None,
               "retry_count": 0, "state": "sending"}
        _jobs[job_id] = job
        asyncio.create_task(_run_send(client, job, saved_guid, mid, recipients, body))
        return {"ok": True, "marker_found": True, "job_id": job_id,
                "total": len(recipients)}

    @app.get("/send/status/{job_id}")
    async def send_status(job_id: str, authorization: str = Header(None)):
        _auth(authorization)
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.post("/send/stop/{job_id}")
    async def send_stop(job_id: str, authorization: str = Header(None)):
        _auth(authorization)
        job = _jobs.get(job_id)
        if job:
            job["stopped"] = True
        return {"stopped": True}

    # ----- automation (rotating texts to the account's groups) -----
    @app.post("/automation/start")
    async def automation_start(body: AutomationIn, authorization: str = Header(None)):
        _auth(authorization)
        # idempotent: stop any existing loop for this phone first
        await _stop_automation(body.phone)
        state = {"stop": False, "sent": 0, "groups": 0, "skipped": set(),
                 "texts": list(body.texts or []),
                 "interval": config.clamp_interval(body.interval), "task": None}
        if not state["texts"]:
            return {"ok": False, "error": "no texts"}
        state["task"] = asyncio.create_task(_run_automation(body.phone, state))
        _automations[rb.normalize_phone(body.phone)] = state
        return {"ok": True}

    @app.post("/automation/stop")
    async def automation_stop(body: AutomationIn, authorization: str = Header(None)):
        _auth(authorization)
        sent = await _stop_automation(body.phone)
        return {"ok": True, "sent": sent}

    @app.get("/automation/status")
    async def automation_status(phone: str, authorization: str = Header(None)):
        _auth(authorization)
        st = _automations.get(rb.normalize_phone(phone))
        if not st:
            return {"running": False, "sent": 0, "groups": 0, "skipped": 0}
        return {"running": not st["stop"], "sent": st["sent"],
                "groups": st["groups"], "skipped": len(st["skipped"])}

    return app


async def _stop_automation(phone: str) -> int:
    """Stop a running automation for a phone; return how many it had sent."""
    st = _automations.pop(rb.normalize_phone(phone), None)
    if not st:
        return 0
    st["stop"] = True
    task = st.get("task")
    if task:
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:
            task.cancel()
    return st.get("sent", 0)


def _pick_text(texts: list, last_idx):
    """Random text index, avoiding the same one as last time (if possible)."""
    if not texts:
        return None, None
    if len(texts) == 1:
        return 0, texts[0]
    choices = [i for i in range(len(texts)) if i != last_idx]
    i = random.choice(choices)
    return i, texts[i]


async def _run_automation(phone: str, state: dict):
    """Worker-side automation loop: every interval, send a random text to each
    group (with a tiny random pause between groups). Skips groups that error."""
    client = rb.open_client(phone)
    last_text: dict = {}
    try:
        await rb.connect_ready(client)
        while not state["stop"]:
            try:
                groups = await rb.get_group_guids(client)
            except Exception:
                groups = []
            state["groups"] = len(groups)
            for g in groups:
                if state["stop"]:
                    break
                guid = g["guid"]
                if guid in state["skipped"]:
                    continue
                idx, txt = _pick_text(state["texts"], last_text.get(guid))
                if txt is None:
                    break
                try:
                    await rb.send_text(client, guid, txt)
                    state["sent"] += 1
                    last_text[guid] = idx
                except Exception:
                    state["skipped"].add(guid)  # mute a failing group
                await asyncio.sleep(random.uniform(
                    config.AUTOMATION_GROUP_DELAY_MIN,
                    config.AUTOMATION_GROUP_DELAY_MAX))
            waited = 0
            while waited < state["interval"] and not state["stop"]:
                await asyncio.sleep(1)
                waited += 1
    except Exception:
        pass
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _sleep_with_stop(job: dict, seconds: float, step: float = 2.0):
    """Sleep up to `seconds`, but bail out early if the job is stopped."""
    waited = 0.0
    while waited < seconds:
        if job.get("stopped"):
            return
        d = min(step, seconds - waited)
        await asyncio.sleep(d)
        waited += d


async def _run_send(client, job: dict, saved_guid, mid, recipients, body):
    """Worker send loop with auto-resume: on hitting max_errors, wait
    body.resume_wait and resume from the rest of the list, up to
    body.max_retries times. Manual stop ends immediately. Calls the UNCHANGED
    rb.forward_message for every recipient."""
    n = len(recipients)
    idx = 0
    try:
        while True:
            attempt_fail = 0
            hit_max = False
            while idx < n:
                if job["stopped"]:
                    job["reason"] = "manual_stop"
                    return
                guid = recipients[idx]
                idx += 1
                try:
                    await asyncio.wait_for(
                        rb.forward_message(client, saved_guid, guid, mid),
                        timeout=body.send_timeout,
                    )
                    job["ok"] += 1
                except Exception as e:  # noqa: BLE001
                    job["fail"] += 1
                    attempt_fail += 1
                    job["last_error"] = repr(e)[:200]
                    if attempt_fail >= body.max_errors:
                        hit_max = True
                        break
                await _sleep_with_stop(job, body.delay)

            if not hit_max:
                break  # finished the whole list
            if job["retry_count"] >= body.max_retries:
                job["reason"] = f"max_errors({body.max_errors})"
                break
            # wait, then reconnect a fresh client and resume from `idx`
            job["retry_count"] += 1
            job["state"] = "waiting"
            await _sleep_with_stop(job, body.resume_wait)
            if job["stopped"]:
                job["reason"] = "manual_stop"
                break
            job["state"] = "sending"
            try:
                await client.disconnect()
            except Exception:
                pass
            client = rb.open_client(body.phone)
            await rb.connect_ready(client)
    except Exception as e:  # noqa: BLE001
        job["reason"] = f"fatal: {repr(e)[:200]}"
    finally:
        job["done"] = True
        try:
            await client.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Entrypoint (called when MODE=worker).
# --------------------------------------------------------------------------- #
def run():
    problems = config.validate_worker()
    if problems:
        print("Missing worker settings in .env: " + ", ".join(problems))
        return
    if not _HAVE_FASTAPI:
        print("نصب نیست: fastapi/uvicorn. اجرا کن: pip install fastapi uvicorn httpx")
        return
    import uvicorn
    app = _build_app()
    host = config.WORKER_BIND_HOST or "0.0.0.0"
    print(f"Worker API listening on {host}:{config.WORKER_API_PORT}", flush=True)
    uvicorn.run(app, host=host, port=config.WORKER_API_PORT, log_level="info")


if __name__ == "__main__":
    run()
