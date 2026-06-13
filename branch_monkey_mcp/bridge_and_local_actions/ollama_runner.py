"""Ollama direct runner — drop-in `claude -p` replacement for local models.

Ollama (https://ollama.com) runs open-weight models — llama, mistral,
gemma, qwen, phi — entirely on the user's own machine. It exposes an
OpenAI-compatible endpoint at `http://localhost:11434/v1`, so this runner
POSTs to `/chat/completions` and translates the OpenAI response shape back
into the claude `--output-format stream-json/text/json` surface the relay
parser already consumes (same trick as gemma_runner / xai_runner).

The whole point: this only works where Ollama is reachable on localhost,
i.e. on the *same machine as the relay*. That's a local-relay compute —
the laptop/server the user runs `cerver`/the relay on. There's no API key
(local Ollama is unauthenticated) and no per-token cost (it's your own
hardware), which is exactly the "40% on open models, ~$0" tier.

Endpoint override: set OLLAMA_BASE_URL (or OLLAMA_HOST) for a non-default
host/port or a remote self-hosted Ollama.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Ollama has no global default model — the caller (relay, from the
# session's metadata.cli_model) supplies one. This is just the floor so
# a bare invocation does something sane if a common small model is pulled.
DEFAULT_MODEL = "llama3.2"
DEFAULT_BASE_URL = "http://localhost:11434/v1"


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _resolve_base_url() -> str:
    """OLLAMA_BASE_URL wins; OLLAMA_HOST (the native Ollama var, e.g.
    `127.0.0.1:11434` or a full URL) is accepted too; else localhost."""
    base = os.environ.get("OLLAMA_BASE_URL")
    if base:
        return base
    host = os.environ.get("OLLAMA_HOST")
    if host:
        host = host.strip()
        if not host.startswith("http"):
            host = f"http://{host}"
        # Native OLLAMA_HOST has no /v1 suffix; the OpenAI-compat surface does.
        return host.rstrip("/") + "/v1"
    return DEFAULT_BASE_URL


def _run() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--prompt", default=None)
    parser.add_argument("--output-format", default="text")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--append-system-prompt", default=None)
    parser.add_argument("--max-tokens", type=int, default=4096)
    # Accept (and ignore) claude-only flags so the relay can pass the same
    # args verbatim without breaking.
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--resume", default=None)
    args, _unknown = parser.parse_known_args()

    prompt = args.prompt
    if prompt is None:
        prompt = sys.stdin.read().strip()
    if not prompt:
        print("ollama_runner: no prompt provided", file=sys.stderr)
        return 2

    base_url = _resolve_base_url()
    # Optional bearer for remote/self-hosted Ollama behind a proxy. Local
    # Ollama ignores it.
    api_key = os.environ.get("OLLAMA_API_KEY", "")

    messages = []
    if args.append_system_prompt:
        messages.append({"role": "system", "content": args.append_system_prompt})
    messages.append({"role": "user", "content": prompt})

    body: dict = {
        "model": args.model,
        "max_tokens": args.max_tokens,
        "messages": messages,
    }

    if args.output_format == "stream-json":
        _emit({
            "type": "system",
            "subtype": "init",
            "session_id": f"ollama-{os.getpid()}",
            "model": args.model,
            "provider": "ollama",
        })

    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )

    try:
        # Local generation on modest hardware can be slow on first token
        # (model load), so allow a generous timeout.
        with urllib.request.urlopen(req, timeout=600) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        # Connection refused → Ollama isn't running. Give an actionable hint.
        reason = getattr(exc, "reason", exc)
        msg = (
            f"Ollama unreachable at {base_url} ({reason}). "
            f"Is it running? Start it with `ollama serve` and pull a model "
            f"with `ollama pull {args.model}`."
        )
        if isinstance(exc, urllib.error.HTTPError):
            err_body = ""
            try:
                err_body = exc.read().decode()
            except Exception:
                pass
            msg = f"Ollama HTTP {exc.code}: {err_body[:600]}"
        if args.output_format == "stream-json":
            _emit({"type": "error", "message": msg})
        else:
            print(msg, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        msg = f"Ollama exception: {exc}"
        if args.output_format == "stream-json":
            _emit({"type": "error", "message": msg})
        else:
            print(msg, file=sys.stderr)
        return 1

    # OpenAI response shape: choices[0].message.content (a string).
    choices = payload.get("choices") or []
    final_text = ""
    if choices and isinstance(choices[0], dict):
        final_text = str((choices[0].get("message") or {}).get("content") or "")

    # OpenAI usage → claude-style usage keys the relay/gateway expect on
    # the `result` event. Ollama reports prompt/completion tokens too.
    raw_usage = payload.get("usage") or {}
    usage = {
        "input_tokens": raw_usage.get("prompt_tokens", 0),
        "output_tokens": raw_usage.get("completion_tokens", 0),
    }

    if args.output_format == "stream-json":
        _emit({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": final_text}]},
        })
        _emit({"type": "result", "result": final_text, "usage": usage})
    elif args.output_format == "json":
        sys.stdout.write(json.dumps({"result": final_text, "usage": usage}))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(final_text + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(_run())
