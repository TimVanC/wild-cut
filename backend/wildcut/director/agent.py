"""Director chat: an agent loop (Messages API with tools) that edits the EDL for Tim.

Each tool call that changes the edit saves an EDL version, so "undo that" works across chat and
manual edits. Conversation turns are persisted (images from look_at are replaced with a short
note before storage) so the next message has context.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlmodel import Session, select

from wildcut.claude import BudgetExceeded, get_client
from wildcut.db import ChatMessage, Project
from wildcut.director.tools import TOOLS, Context, run_tool, summarize_clips, summarize_edit
from wildcut.services.planning import current_edl, load_moments, move_cursor, plan_project
from wildcut.services.projects import budget_for, project_clips

log = logging.getLogger(__name__)
MAX_ROUNDS = 12
HISTORY_MESSAGES = 24

SYSTEM = """You are the Director for Wild Cut, a short-form wildlife edit tool. Tim directs the edit in plain language and you make the changes with tools. The edit decision list (EDL) is the single source of truth; every tool change is saved as a version so Tim can undo.

Rules:
- Use tools for every change; never claim a change you did not make.
- Clip references: numbers ("clip 2"), descriptions ("the falcon one"), or timestamps ("at 0:04 in clip 3" = source time 4 s of clip 3). If a reference is genuinely ambiguous, ask one short question instead of guessing.
- To find a described moment ("when it lets go of the branch"), use look_at on the likely range, pick the frame time, then act on it with {clip, source_time}.
- Text rule: in a single-animal edit the only text is the animal's name as the title ("THE GIBBON"). Never add slogans, captions or hype text. Showdown is the only format with other text.
- Anything Tim specifies is locked; call plan_auto(scope="fill") afterwards when Tim asks to fill in the rest or when the edit would otherwise be incomplete.
- Music terms resolve against the beat grid: "the drop", "second bass hit" = bass_hit:2, "third downbeat" = downbeat:3.
- "undo that" = the undo tool.
- A message starting with "BRIEF:" is the direction Tim wrote before the first edit was built. An auto edit already exists; apply every concrete instruction in it with tools: the moments he names with timestamps (source times in the clip, "1:42" = 102 s) via set_hero for the biggest one, set_clip_range / insert_clip / set_order for the others; the title via set_title; the mood via set_style, set_intensity and set_frame; parts to avoid via remove_clip. Use look_at when a description needs checking. Leave what the brief does not mention to the auto edit. Finish with plan_auto(scope="fill") and a short summary of what you placed where.
- After the changes, reply with one or two short lines summarizing exactly what you did (and the resulting order or time when relevant). No preamble, no questions unless something is ambiguous.
"""


def _history(s: Session, project_id: str) -> list[dict]:
    rows = s.exec(select(ChatMessage).where(ChatMessage.project_id == project_id).order_by(ChatMessage.created_at)).all()
    msgs = []
    for r in rows[-HISTORY_MESSAGES:]:
        if r.role in ("user", "assistant") and r.blocks:
            msgs.append({"role": r.role, "content": r.blocks})
    # the API requires alternating roles starting with a user turn
    cleaned: list[dict] = []
    for m in msgs:
        if cleaned and cleaned[-1]["role"] == m["role"]:
            cleaned[-1]["content"] = list(cleaned[-1]["content"]) + list(m["content"])
        else:
            cleaned.append(m)
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    return cleaned


def _strip_images(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
            kept = [x for x in b["content"] if x.get("type") != "image"]
            n_img = len(b["content"]) - len(kept)
            if n_img:
                kept.append({"type": "text", "text": f"[{n_img} frames were shown]"})
            out.append({**b, "content": kept})
        else:
            out.append(b)
    return out


def _block_to_dict(block: Any) -> dict:
    if hasattr(block, "model_dump"):
        d = block.model_dump(exclude_none=True)
    else:
        d = dict(block)
    # keep only the API-replayable fields
    if d.get("type") == "text":
        return {"type": "text", "text": d.get("text", "")}
    if d.get("type") == "tool_use":
        return {"type": "tool_use", "id": d["id"], "name": d["name"], "input": d.get("input", {})}
    if d.get("type") == "thinking":
        return {"type": "thinking", "thinking": d.get("thinking", ""), "signature": d.get("signature", "")}
    return d


def _save(s: Session, project_id: str, role: str, content: str, blocks: list[dict], version: int | None) -> ChatMessage:
    row = ChatMessage(project_id=project_id, role=role, content=content, blocks=blocks, edl_version=version)
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def context_block(ctx: Context) -> str:
    p = ctx.project
    return ("Project: " + json.dumps({"name": p.name, "style": p.style, "aspect": p.aspect, "mode": p.mode, "target_length": p.target_length,
                                     "audio_export": p.audio_export}) +
            "\nClips: " + json.dumps(summarize_clips(ctx), default=str) +
            "\nEdit: " + json.dumps(summarize_edit(ctx), default=str))


def run_turn(s: Session, project: Project, message: str, progress=None) -> dict:
    """One user message -> tool calls -> assistant reply. Returns {reply, edl_changed, versions, log}."""
    message = (message or "").strip()
    row = current_edl(s, project)
    if row is None:
        plan_project(s, project, keep_locks=False, note="chat: initial plan")
        row = current_edl(s, project)
    ctx = Context(s=s, project=project, edl=row.json if row else {}, clips=project_clips(s, project.id),
                  moments=load_moments(s, project.id))
    _save(s, project.id, "user", message, [{"type": "text", "text": message}], row.version if row else None)
    client = get_client()
    if not client.enabled:
        reply = _fallback(ctx, message)
        _save(s, project.id, "assistant", reply, [{"type": "text", "text": reply}], ctx.versions[-1] if ctx.versions else None)
        return {"reply": reply, "edl_changed": ctx.changed, "versions": ctx.versions, "log": ctx.log}
    budget = budget_for(s, project)
    history = _history(s, project.id)
    # the latest user turn carries the live context so earlier turns stay cache-stable
    history[-1] = {"role": "user", "content": [{"type": "text", "text": context_block(ctx) + "\n\nTim: " + message}]}
    messages = history
    reply_text = ""
    try:
        for round_no in range(MAX_ROUNDS):
            if progress:
                progress(min(0.9, 0.1 + round_no * 0.07), "thinking")
            response = client.chat(SYSTEM, messages, tools=TOOLS, budget=budget, max_tokens=3000, note="director")
            blocks = [_block_to_dict(b) for b in response.content]
            messages.append({"role": "assistant", "content": blocks})
            texts = [b["text"] for b in blocks if b.get("type") == "text"]
            if response.stop_reason == "refusal":
                reply_text = "I can't help with that request."
                break
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not tool_uses:
                reply_text = "\n".join(texts).strip()
                break
            results = []
            for tu in tool_uses:
                content, is_error = run_tool(ctx, tu["name"], tu.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            if progress:
                progress(min(0.9, 0.2 + round_no * 0.07), f"{', '.join(t['name'] for t in tool_uses)}")
        else:
            reply_text = "I made the changes above but ran out of steps; say 'continue' to keep going."
    except BudgetExceeded as e:
        reply_text = f"Stopped: {e}. Raise CLAUDE_BUDGET_PER_PROJECT_USD to continue."
    except Exception as e:  # noqa: BLE001
        log.exception("director turn failed")
        if len(messages) == len(history):
            # Claude never answered: do what the offline command set can, and say why
            reply_text = _fallback(ctx, message) + f" (Claude error: {str(e)[:200]})"
        else:
            reply_text = f"Something went wrong talking to Claude: {str(e)[:300]}"
    # persist the assistant turns and tool results (without image payloads)
    for m in messages[len(history):]:
        if m["role"] == "assistant":
            text = "\n".join(b["text"] for b in m["content"] if b.get("type") == "text")
            _save(s, project.id, "assistant", text, m["content"], ctx.versions[-1] if ctx.versions else None)
        else:
            _save(s, project.id, "tool", "", _strip_images(m["content"]), ctx.versions[-1] if ctx.versions else None)
    if not reply_text:
        reply_text = "Done: " + "; ".join(ctx.log) if ctx.log else "No changes made."
    # make sure a visible assistant row carries the final reply text
    last_assistant = next((m for m in reversed(messages[len(history):]) if m["role"] == "assistant"), None)
    last_text = " ".join(b["text"] for b in last_assistant["content"] if b.get("type") == "text") if last_assistant else ""
    if last_text.strip() != reply_text.strip():
        _save(s, project.id, "assistant", reply_text, [{"type": "text", "text": reply_text}], ctx.versions[-1] if ctx.versions else None)
    return {"reply": reply_text, "edl_changed": ctx.changed, "versions": ctx.versions, "log": ctx.log}


def _fallback(ctx: Context, message: str) -> str:
    """Tiny offline command set when Claude is not configured."""
    m = message.lower()
    if "undo" in m:
        row = move_cursor(ctx.s, ctx.project, -1)
        if row:
            ctx.edl = row.json
            ctx.changed = True
            return f"Undid to version {row.version}."
        return "Nothing to undo."
    order = re.findall(r"clip\s*(\d+)", m)
    if order and ("first" in m or "then" in m or "order" in m):
        res, err = run_tool(ctx, "set_order", {"clips": [int(x) for x in order]})
        return "Order set: " + ", ".join(f"Clip {x}" for x in order) if not err else str(res)
    return ("Claude is not configured (ANTHROPIC_API_KEY / ANTHROPIC_WORKSPACE_ID), so the Director can only do "
            "'undo' and 'clip 2 first, then clip 3' style ordering offline.")
