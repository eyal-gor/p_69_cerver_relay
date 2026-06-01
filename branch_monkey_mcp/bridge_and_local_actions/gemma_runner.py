"""Gemma direct runner — drop-in `claude -p` replacement for Gemma routing.

Gemma is Google's open-weights model, served free (rate-limited) on the
Gemini API. Unlike xAI, Google does NOT expose an Anthropic-compatible
`/v1/messages` surface, so we can't reuse xai_runner's request shape.
Google *does* expose an OpenAI-compatible endpoint at
`generativelanguage.googleapis.com/v1beta/openai`, so this runner POSTs
to `/chat/completions` and then translates the OpenAI response shape
back into the claude `--output-format stream-json/text/json` surface the
relay parser already consumes. No claude binary, no model download — the
weights live on Google's side; we just need a GEMINI_API_KEY.

One Gemma quirk: its chat template has no dedicated system turn, and the
OpenAI-compat endpoint rejects a `system` role for Gemma models. We fold
any system prompt into the first user message instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemma-3-27b-it"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _sanitize_key(raw: str) -> str:
    """Mirror xai_runner's defence against vault values that picked up
    non-ASCII or bracketed-paste escape leftovers. Gemini keys look like
    `AIza` + 35 url-safe chars; extract that run, else fall back to a
    printable-ASCII strip."""
    m = re.search(r"AIza[0-9A-Za-z_\-]{30,}", raw)
    if m:
        return m.group(0)
    return "".join(ch for ch in raw if 0x20 <= ord(ch) < 0x7f).strip()


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
        print("gemma_runner: no prompt provided", file=sys.stderr)
        return 2

    raw_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not raw_key:
        print(
            "gemma_runner: no API key (set GEMINI_API_KEY or GOOGLE_API_KEY)",
            file=sys.stderr,
        )
        return 2
    api_key = _sanitize_key(raw_key)
    if not api_key:
        msg = "gemma_runner: API key empty/unparseable after sanitization — vault value is malformed"
        if args.output_format == "stream-json":
            _emit({"type": "error", "message": msg})
        else:
            print(msg, file=sys.stderr)
        return 2
    if api_key != raw_key:
        sys.stderr.write(
            f"gemma_runner: sanitized API key ({len(raw_key)} -> {len(api_key)} bytes). "
            f"Rotate the value in your vault to clean it up properly.\n"
        )

    base_url = os.environ.get("GEMINI_BASE_URL") or DEFAULT_BASE_URL

    # Gemma has no system role — prepend the system prompt to the user turn.
    content = prompt
    if args.append_system_prompt:
        content = f"{args.append_system_prompt}\n\n{prompt}"

    body: dict = {
        "model": args.model,
        "max_tokens": args.max_tokens,
        "messages": [{"role": "user", "content": content}],
    }

    if args.output_format == "stream-json":
        _emit({
            "type": "system",
            "subtype": "init",
            "session_id": f"gemma-{os.getpid()}",
            "model": args.model,
            "provider": "google",
        })

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode()
        except Exception:
            pass
        msg = f"Gemma HTTP {exc.code}: {err_body[:600]}"
        if args.output_format == "stream-json":
            _emit({"type": "error", "message": msg})
        else:
            print(msg, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        msg = f"Gemma exception: {exc}"
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
    # the `result` event (input_tokens / output_tokens).
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
