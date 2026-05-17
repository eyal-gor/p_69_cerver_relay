"""
Execution helpers for the reusable computer runtime.

This module does not own the full agent/session lifecycle. It only owns
portable execution mechanics such as:
- prompt construction
- normalized output handling
- listener broadcasting
- result extraction
"""

import json
from typing import Any, Dict, Optional


def build_agent_prompt(
    prompt: Optional[str],
    task_id: Optional[str],
    task_number: Optional[int],
    task_title: str,
    task_description: Optional[str],
    target_branch: Optional[str],
    worktree_path: Optional[str],
) -> str:
    """Build the final prompt, prepending worktree/workspace info if applicable."""
    if prompt:
        final_prompt = prompt
        if worktree_path:
            worktree_info = f"""## IMPORTANT: Worktree Already Created
You are working in an isolated git worktree at: `{worktree_path}`
Branch: `{target_branch}`

Do NOT create another worktree - you are already isolated. Skip any worktree creation steps.

---

"""
            final_prompt = worktree_info + final_prompt
        return final_prompt

    task_json = {
        "task_uuid": task_id,
        "task_number": task_number,
        "title": task_title or "Untitled task",
        "description": task_description or "",
        "branch": target_branch,
        "worktree_path": str(worktree_path) if worktree_path else None,
    }
    return f"""Please start working on this task:

```json
{json.dumps(task_json, indent=2)}
```"""


def process_provider_output_text(agent: Any, provider: Any, text: str) -> Optional[Dict[str, Any]]:
    """Normalize one line of provider output and update agent state as needed."""
    try:
        parsed = json.loads(text)
        normalized = provider.normalize_event(parsed)
        if normalized is None:
            return None

        session_id = provider.extract_session_id(parsed)
        if session_id:
            agent.session_id = session_id

        normalized_text = json.dumps(normalized)
        agent.output_buffer.append({"data": normalized_text, "parsed": normalized})
        if len(agent.output_buffer) > 1000:
            agent.output_buffer.pop(0)

        return {
            "type": "output",
            "data": normalized_text,
            "raw": text,
        }
    except json.JSONDecodeError:
        if provider.is_noise(text):
            return None
        return {
            "type": "output",
            "data": text,
        }


async def broadcast_to_agent_listeners(agent: Any, event: Dict[str, Any]) -> None:
    """Broadcast an event to all registered listeners for an agent."""
    for queue in agent.output_listeners:
        try:
            await queue.put(event)
        except Exception:
            pass


def extract_result_from_output_buffer(output_buffer: list) -> str:
    """Extract the final result text from a normalized output buffer."""
    for item in reversed(output_buffer):
        parsed = item.get("parsed") if isinstance(item, dict) else None
        if not parsed:
            continue
        if parsed.get("type") == "result":
            result = parsed.get("result", "")
            if isinstance(result, str) and result.strip():
                return result

    text_parts = []
    for item in output_buffer:
        parsed = item.get("parsed") if isinstance(item, dict) else None
        if not parsed:
            continue
        if parsed.get("type") == "assistant":
            message = parsed.get("message", {})
            for block in message.get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

    return "\n\n".join(text_parts) if text_parts else ""


__all__ = [
    "build_agent_prompt",
    "process_provider_output_text",
    "broadcast_to_agent_listeners",
    "extract_result_from_output_buffer",
]
