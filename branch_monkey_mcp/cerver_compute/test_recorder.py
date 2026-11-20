"""Run: uv run python -m branch_monkey_mcp.cerver_compute.test_recorder

A fake gateway + fake Neon Data API on a local port. Checks: entries go
straight to "Neon" with the pass, the pass is reused, a refused pass is
dropped, and recording-off falls back (returns False) without retrying.
"""
import asyncio, socket, threading, time

import httpx, uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import recorder

app = FastAPI()
state = {"mode": "on", "passes": 0, "rows": [], "neon_status": 200}


@app.post("/v2/sessions/{sid}/recorder")
async def pass_(sid: str):
    if state["mode"] == "off":
        return JSONResponse({"error": "Direct recording is off"}, status_code=404)
    state["passes"] += 1
    return {"url": f"{BASE}/rpc/cerver_record_lines", "token": f"t{state['passes']}", "expires_at": "2099-01-01T00:00:00Z"}


@app.post("/rpc/cerver_record_lines")
async def rpc(req: Request):
    body = await req.json()
    if state["neon_status"] != 200:
        return JSONResponse({"message": "nope"}, status_code=state["neon_status"])
    state["rows"].append((req.headers["authorization"], body))
    return 2


def port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


P = port(); BASE = f"http://127.0.0.1:{P}"


async def main():
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=P, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(BASE + "/docs"); break
        except Exception:
            time.sleep(0.1)
    fails = []
    def check(ok, msg):
        print(("  ok   " if ok else "  FAIL ") + msg); (None if ok else fails.append(msg))
    t = {"url": BASE, "token": "ck_x", "session_id": "sess_1"}
    e = [{"role": "assistant", "kind": "text", "content": "hello " * 1000}]

    check(await recorder.record_direct(t, e) is True, "entries land in Neon directly")
    check(state["rows"][-1][0] == "Bearer t1" and state["rows"][-1][1]["p_session_id"] == "sess_1", "sent with the session's pass")
    check(bool(state["rows"][-1][1]["p_entries"][0].get("at")), "each entry gets a time")
    await recorder.record_direct(t, e)
    check(state["passes"] == 1, "the pass is reused")
    state["neon_status"] = 401
    check(await recorder.record_direct(t, e) is False, "a refused pass falls back to the gateway")
    state["neon_status"] = 200
    await recorder.record_direct(t, e)
    check(state["passes"] == 2, "a refused pass is replaced")
    recorder.forget("sess_2"); state["mode"] = "off"
    t2 = dict(t, session_id="sess_2")
    check(await recorder.record_direct(t2, e) is False, "recording off falls back")
    n = state["passes"]; await recorder.record_direct(t2, e)
    check(state["passes"] == n, "recording off isn't asked again right away")
    server.should_exit = True
    print("recorder: " + ("PASS" if not fails else f"FAIL ({len(fails)})"))
    return 1 if fails else 0

raise SystemExit(asyncio.run(main()))
