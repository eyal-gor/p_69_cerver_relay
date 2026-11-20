"""Direct mode test. Run: uv run python -m branch_monkey_mcp.bridge_and_local_actions.routes.test_p2p

Starts the relay's p2p route on a local port, opens a real WebRTC data channel
to it (aiortc on both sides), and checks: a command runs and its output comes
back over the channel, a guest's command is still fenced, a guest can't attach
to an unfenced sandbox, and an unknown sandbox is refused.
"""
import asyncio, json, os, socket, threading, time

import httpx, uvicorn
from fastapi import FastAPI
from aiortc import RTCPeerConnection, RTCSessionDescription

from ...cerver_compute import shell_sandbox as shell
from . import p2p

app = FastAPI(); app.include_router(p2p.router, prefix="/api/p2p")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


async def connect(base, sandbox_id, guest=False):
    pc = RTCPeerConnection()
    ch = pc.createDataChannel("cerver")
    replies, opened = asyncio.Queue(), asyncio.Event()
    ch.on("open", opened.set)
    ch.on("message", lambda m: replies.put_nowait(json.loads(m)))
    await pc.setLocalDescription(await pc.createOffer())
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{base}/api/p2p/offer", json={"sdp": pc.localDescription.sdp, "type": "offer", "sandbox_id": sandbox_id, "guest": guest}, timeout=30)
    if r.status_code != 200:
        await pc.close(); return None, r.status_code, None
    a = r.json()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=a["sdp"], type="answer"))
    await asyncio.wait_for(opened.wait(), 15)
    async def ask(code):
        ch.send(json.dumps({"id": str(time.time()), "code": code, "timeout": 30}))
        return await asyncio.wait_for(replies.get(), 40)
    return pc, 200, ask


async def main():
    port = free_port(); base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(f"{base}/api/p2p/status"); break
        except Exception:
            time.sleep(0.1)
    fails = []
    def check(ok, msg):
        print(("  ok   " if ok else "  FAIL ") + msg); (None if ok else fails.append(msg))

    owner = shell.create_shell_sandbox({"working_dir": os.path.expanduser("~")}, None)
    guest = shell.create_shell_sandbox({"pool_session": True}, None)
    t0 = time.monotonic()
    pc, code, ask = await connect(base, owner["sandbox_id"])
    check(code == 200, f"channel opens ({round((time.monotonic()-t0)*1000)} ms)")
    if ask:
        r = await ask("echo hello-direct && whoami")
        check(r.get("exit_code") == 0 and "hello-direct" in r.get("stdout", ""), f"owner command runs over the channel: {r.get('stdout','').strip().replace(chr(10),' / ')}")
        await pc.close()
    pc, code, ask = await connect(base, guest["sandbox_id"], guest=True)
    if ask:
        r = await ask(f"cat {os.path.expanduser('~')}/.zshrc >/dev/null 2>&1 && echo LEAK || echo blocked; pwd")
        check("blocked" in r.get("stdout", "") and "LEAK" not in r.get("stdout", ""), f"guest is still fenced: {r.get('stdout','').strip().replace(chr(10),' / ')}")
        await pc.close()
    else:
        check(False, f"guest channel failed ({code})")
    _, code, _ = await connect(base, owner["sandbox_id"], guest=True)
    check(code == 403, f"guest can't attach to an unfenced sandbox ({code})")
    _, code, _ = await connect(base, "shell_nope")
    check(code == 404, f"unknown sandbox refused ({code})")
    shell.delete_shell_sandbox(owner["sandbox_id"]); shell.delete_shell_sandbox(guest["sandbox_id"])
    server.should_exit = True
    print("p2p: " + ("PASS" if not fails else f"FAIL ({len(fails)})"))
    return 1 if fails else 0

raise SystemExit(asyncio.run(main()))
