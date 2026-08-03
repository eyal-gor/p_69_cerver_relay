"""Ollama direct runner — drop-in `claude -p` replacement for local models.

Ollama serves whatever weights you have pulled on *this machine* over an
OpenAI-compatible endpoint at `localhost:11434/v1`. That makes it the same
shape as gemma_runner: POST `/chat/completions`, translate the OpenAI
response back into the claude `--output-format stream-json/text/json`
surface the relay parser already consumes.

What's different from every other provider here: there is no API key and no
vendor account. The models are files on the user's disk, so which models
exist is a property of the machine, not of cerver — see
`list_local_models()`, which is what the relay reports upward so the
picker can offer this machine's actual models rather than a guess.

Local models are also slow to first token when the weights have to be read
from disk, hence the generous default timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import List

DEFAULT_BASE_URL = "http://localhost:11434"
# No sensible universal default — which model exists is per-machine. We
# resolve one at call time from what's actually pulled.
DEFAULT_MODEL = ""
# Cold start on a big local model can be minutes: the weights stream off
# disk before the first token appears.
DEFAULT_TIMEOUT = 600


def _base_url() -> str:
    return (os.environ.get("OLLAMA_HOST") or DEFAULT_BASE_URL).rstrip("/")


def list_local_models(timeout: float = 2.0) -> List[str]:
    """Return the models pulled on this machine, newest-looking first.

    Hits Ollama's native `/api/tags`. Returns [] when the server isn't
    running — callers treat that as "this machine can't do ollama", which
    is exactly right.
    """
    try:
        with urllib.request.urlopen(f"{_base_url()}/api/tags", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — server down, not installed, blocked port
        return []
    names: List[str] = []
    for m in payload.get("models") or []:
        name = m.get("name") or m.get("model")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


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

    available = list_local_models()
    if not available:
        msg = (
            f"ollama_runner: no Ollama server at {_base_url()} "
            "(start it, or pull a model with `ollama pull llama3.2`)"
        )
        if args.output_format == "stream-json":
            _emit({"type": "error", "message": msg})
        else:
            print(msg, file=sys.stderr)
        return 2

    # Resolve the model against what's actually here. An exact match wins;
    # otherwise accept a bare name for a tagged model ("llama3.2" →
    # "llama3.2:latest") so callers don't have to know local tag suffixes.
    model = (args.model or "").strip()
    if not model:
        model = available[0]
    elif model not in available:
        prefixed = [m for m in available if m.split(":", 1)[0] == model.split(":", 1)[0]]
        if prefixed:
            model = prefixed[0]
        else:
            msg = (
                f"ollama_runner: model '{model}' is not pulled on this machine. "
                f"Available: {', '.join(available)}"
            )
            if args.output_format == "stream-json":
                _emit({"type": "error", "message": msg})
            else:
                print(msg, file=sys.stderr)
            return 2

    messages = []
    if args.append_system_prompt:
        messages.append({"role": "system", "content": args.append_system_prompt})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model,
        "max_tokens": args.max_tokens,
        "messages": messages,
    }

    if args.output_format == "stream-json":
        _emit({
            "type": "system",
            "subtype": "init",
            "session_id": f"ollama-{os.getpid()}",
            "model": model,
            "provider": "ollama",
        })

    req = urllib.request.Request(
        f"{_base_url()}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )

    try:
        timeout = int(os.environ.get("OLLAMA_TIMEOUT") or DEFAULT_TIMEOUT)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode()
        except Exception:  # noqa: BLE001
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

    choices = payload.get("choices") or []
    final_text = ""
    if choices and isinstance(choices[0], dict):
        final_text = str((choices[0].get("message") or {}).get("content") or "")

    # OpenAI usage → the claude-style keys the relay/gateway read off the
    # `result` event. Local inference is free, but the counts still drive
    # transcript stats, so report them honestly.
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
