"""xAI direct runner — drop-in replacement for `claude -p` on grok routing.

The previous GrokProvider repurposed the `claude` binary with
`ANTHROPIC_BASE_URL=https://api.x.ai` to talk to xAI. That works as
plumbing but joins grok at the hip with claude's subscription rate
limit: when the Anthropic Max quota is exhausted, the claude binary
returns "You have reached your specified API usage limits" *before*
the HTTP request to xAI is made — so grok dies even though xAI has
nothing to do with it.

This runner removes that coupling. It speaks the same CLI surface
claude's --output-format stream-json/text speaks (the bits the relay
parser actually consumes), but does its own HTTP call to xAI's
Anthropic-compatible /v1/messages endpoint. No claude binary in the
loop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _run() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--prompt", default=None)
    parser.add_argument("--output-format", default="text")
    parser.add_argument("--model", default="grok-4")
    parser.add_argument("--append-system-prompt", default=None)
    parser.add_argument("--max-tokens", type=int, default=4096)
    # Accept (and ignore) claude-only flags so the relay can pass the
    # same args verbatim without breaking.
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--resume", default=None)
    args, _unknown = parser.parse_known_args()

    prompt = args.prompt
    if prompt is None:
        prompt = sys.stdin.read().strip()
    if not prompt:
        print("xai_runner: no prompt provided", file=sys.stderr)
        return 2

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("XAI_API_KEY")
    if not api_key:
        print("xai_runner: no API key (set ANTHROPIC_API_KEY or XAI_API_KEY)", file=sys.stderr)
        return 2

    # Sanitize. Two layers of damage we've seen on the vault value
    # for XAI_API_KEY:
    #   1. Non-ASCII (↓, smart quotes, non-breaking space). HTTP
    #      header values must encode to latin-1; non-ASCII bricks
    #      urllib with an opaque codec error *inside* the http call,
    #      after the gateway has already accepted the session.
    #   2. Terminal escape leftovers (\x1b[?2004h, \x1b[?1004h paste-
    #      mode codes) baked into the visible characters. These get
    #      pasted in when the user copies from a terminal with
    #      bracketed-paste enabled and the escape sequence's printable
    #      tail (`[?2004h`) ends up alongside the key text.
    #
    # Strategy: prefer to extract a contiguous run that looks like an
    # xAI key (`xai-` + 40+ alphanumeric/-/_ chars). Fall back to a
    # general printable-ASCII strip if the regex finds nothing.
    import re
    extracted = None
    m = re.search(r"xai-[A-Za-z0-9_-]{40,}", api_key)
    if m:
        extracted = m.group(0)
    else:
        # No clear xai-pattern — fall back to printable-ASCII strip and
        # hope the underlying value is just a non-standard format.
        extracted = "".join(ch for ch in api_key if 0x20 <= ord(ch) < 0x7f).strip()
    if not extracted:
        msg = "xai_runner: API key empty/unparseable after sanitization — vault value is malformed"
        if args.output_format == "stream-json":
            _emit({"type": "error", "message": msg})
        else:
            print(msg, file=sys.stderr)
        return 2
    if extracted != api_key:
        # Don't reveal the secret — just say what was dropped so a
        # silently-corrected key still surfaces during debugging.
        sys.stderr.write(
            f"xai_runner: sanitized API key ({len(api_key)} -> {len(extracted)} bytes). "
            f"Rotate the value in your vault to clean it up properly.\n"
        )
    api_key = extracted

    base_url = os.environ.get("ANTHROPIC_BASE_URL") or "https://api.x.ai"
    body: dict = {
        "model": args.model,
        "max_tokens": args.max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if args.append_system_prompt:
        body["system"] = args.append_system_prompt

    if args.output_format == "stream-json":
        _emit({
            "type": "system",
            "subtype": "init",
            "session_id": f"xai-{os.getpid()}",
            "model": args.model,
            "provider": "xai",
        })

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
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
        msg = f"xAI HTTP {exc.code}: {err_body[:600]}"
        if args.output_format == "stream-json":
            _emit({"type": "error", "message": msg})
        else:
            print(msg, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        msg = f"xAI exception: {exc}"
        if args.output_format == "stream-json":
            _emit({"type": "error", "message": msg})
        else:
            print(msg, file=sys.stderr)
        return 1

    # Concatenate the text content blocks into one string. xAI's
    # Anthropic-compat response shape mirrors Anthropic's: `content`
    # is a list of `{"type":"text","text":"..."}` blocks.
    text_parts = []
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
    final_text = "".join(text_parts)

    if args.output_format == "stream-json":
        _emit({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": final_text}]},
        })
        _emit({
            "type": "result",
            "result": final_text,
            "usage": payload.get("usage", {}),
        })
    elif args.output_format == "json":
        sys.stdout.write(json.dumps({"result": final_text, "usage": payload.get("usage", {})}))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(final_text + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(_run())
