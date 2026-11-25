"""Local models for apps you connected: the gateway forwards an OpenAI-style
chat request here, and we hand it to Ollama on this machine.

POST /api/models/chat   body = OpenAI chat request (model = an Ollama name)
GET  /api/models        the models pulled here

Only the gateway (over this computer's own connection, after checking the
request is from this computer's owner) and this machine can reach it.
Ollama's own OpenAI-compatible endpoint does the work; nothing is sent
anywhere else.
"""

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..ollama_runner import _base_url, list_local_models

router = APIRouter()

# A cold start loads the weights off disk first; give it time.
TIMEOUT_S = 170.0


@router.get("")
async def models():
    return {"models": list_local_models()}


@router.post("/chat")
async def chat(request: Request):
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
        raise HTTPException(400, "model is required")
    if body["model"] not in list_local_models() and f'{body["model"]}:latest' not in list_local_models():
        raise HTTPException(404, f'{body["model"]} is not pulled on this computer')
    body["stream"] = False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post(f"{_base_url()}/v1/chat/completions", json=body)
    except httpx.TimeoutException:
        raise HTTPException(504, "The model took too long to answer")
    except httpx.HTTPError:
        raise HTTPException(503, "Ollama isn't running on this computer")
    try:
        data = r.json()
    except ValueError:
        raise HTTPException(502, f"Ollama answered {r.status_code}")
    if r.status_code >= 400:
        msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        raise HTTPException(r.status_code, msg or f"Ollama answered {r.status_code}")
    return data
