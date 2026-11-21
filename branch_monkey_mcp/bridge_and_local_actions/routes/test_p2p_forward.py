"""Port forwarding test. Run: uv run python -m branch_monkey_mcp.bridge_and_local_actions.routes.test_p2p_forward

Starts the relay's p2p route and a small web app on two local ports, opens a
real WebRTC connection to /api/p2p/forward (aiortc on both sides), and checks:
an HTTP request to the app goes through a "tcp:<port>" channel and its answer
comes back, a 2 MB body arrives whole, two channels work at once, a port with
nothing listening closes the channel, and a bad label is refused.
"""
import asyncio, hashlib, socket, threading, time

import httpx, uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from aiortc import RTCPeerConnection, RTCSessionDescription

from . import p2p

relay = FastAPI(); relay.include_router(p2p.router, prefix="/api/p2p")
BIG = bytes(range(256)) * 8192  # 2 MB
web = FastAPI()


@web.get("/")
def hello():
    return {"hello": "from the app"}


@web.get("/big")
def big():
    return Response(BIG, media_type="application/octet-stream")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError:
            time.sleep(0.1)
    return server


async def fetch(pc, port, path):
    """Send one HTTP/1.0 request through a new tcp:<port> channel; return the raw reply."""
    ch = pc.createDataChannel(f"tcp:{port}")
    opened, closed, chunks = asyncio.Event(), asyncio.Event(), []
    ch.on("open", opened.set)
    ch.on("close", closed.set)
    ch.on("message", lambda m: chunks.append(m if isinstance(m, bytes) else m.encode()))
    await asyncio.wait_for(opened.wait(), 15)
    ch.send(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
    await asyncio.wait_for(closed.wait(), 30)
    return b"".join(chunks)


async def main():
    rport, wport = free_port(), free_port()
    r_srv, w_srv = serve(relay, rport), serve(web, wport)
    fails = []
    def check(ok, msg):
        print(("  ok   " if ok else "  FAIL ") + msg); (None if ok else fails.append(msg))

    check(p2p.forward_port("tcp:3456") == 3456 and p2p.forward_port("tcp:0") is None and p2p.forward_port("cerver") is None, "labels parse")

    pc = RTCPeerConnection()
    pc.createDataChannel("hello")  # an offer needs one channel up front
    await pc.setLocalDescription(await pc.createOffer())
    t0 = time.monotonic()
    async with httpx.AsyncClient() as c:
        r = await c.post(f"http://127.0.0.1:{rport}/api/p2p/forward", json={"sdp": pc.localDescription.sdp, "type": "offer"}, timeout=30)
    check(r.status_code == 200, f"machine answers the offer ({r.status_code})")
    await pc.setRemoteDescription(RTCSessionDescription(sdp=r.json()["sdp"], type="answer"))

    reply = await fetch(pc, wport, "/")
    check(b"200 OK" in reply and b"from the app" in reply, f"web app answers through the channel ({round((time.monotonic()-t0)*1000)} ms incl. connect)")

    t1 = time.monotonic()
    reply = await fetch(pc, wport, "/big")
    body = reply.split(b"\r\n\r\n", 1)[-1]
    ms = (time.monotonic() - t1) * 1000
    check(hashlib.sha256(body).digest() == hashlib.sha256(BIG).digest(), f"2 MB arrives whole ({len(body)} bytes, {round(ms)} ms)")

    a, b = await asyncio.gather(fetch(pc, wport, "/"), fetch(pc, wport, "/"))
    check(b"from the app" in a and b"from the app" in b, "two connections at once")

    reply = await asyncio.wait_for(fetch(pc, free_port(), "/"), 20)
    check(reply == b"", "nothing listening: channel closes, no data")

    await pc.close()
    r_srv.should_exit = w_srv.should_exit = True
    print("p2p forward: " + ("PASS" if not fails else f"FAIL ({len(fails)})"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
