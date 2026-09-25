"""Optional Foundation Model pass over the translated code objects.

Everything else in the bundle is derived deterministically, which is what makes it
safe to act on. This is the one part a model writes, and it earns its place for a
job rules cannot do: reading a procedure's original T-SQL beside the PostgreSQL
that replaced it and saying what a *caller* has to change — whether rows still
come back, whether a parameter became INOUT, which side effect moved.

Same endpoint, wire negotiation and retry policy as the rest of the app (see
``fm_params``), and the same fail-soft contract as ``assessment/ai_analysis``: any
error returns ``success=False`` so the deterministic export still renders. The
endpoint that produced the notes is carried on the result and shown wherever they
are, so a reader always knows which model wrote them.
"""
from __future__ import annotations

import json
import logging
import re

from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

from backend.config import FM_ENDPOINT
from backend.context_bundle.models import AiNotes, AiObjectNote
from backend.fm_params import chat_text, query_chat
from backend.migration.models import ObjectKind, PlanItem
from backend.projects.models import Project
from backend.prompts import render

log = logging.getLogger("lakebase_express.context_ai_notes")

# Objects the model is asked about, and how much of each body it sees. Bounded so
# the prompt stays the same size whatever the source looks like.
_MAX_OBJECTS = 20
_MAX_DEF_CHARS = 1500

# Reasoning models spend thinking tokens from the same budget; ask big and let
# query_chat clamp to the endpoint's real output window.
_MAX_OUTPUT_TOKENS = 128000

_CODE_KINDS = frozenset(
    {ObjectKind.PROCEDURE, ObjectKind.VIEW, ObjectKind.FUNCTION, ObjectKind.TRIGGER}
)

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "app_migration_notes",
        "schema": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "call_site": {"type": "string"},
                            "behaviour": {"type": "string"},
                            "watch_out": {"type": "string"},
                        },
                        "required": ["source", "call_site", "behaviour", "watch_out"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["notes"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def _system_prompt() -> str:
    return render(__file__, "app_migration_notes.system.jinja")


def code_items(project: Project) -> list[PlanItem]:
    """Translated code objects from the project's plan, newest translation first.

    Only objects with target SQL: an untranslated one has nothing to compare, and
    the skill already says the application must supply that logic itself.
    """
    items: list[PlanItem] = []
    for raw in project.plan or []:
        try:
            item = PlanItem.model_validate(raw)
        except Exception:
            continue
        if item.kind in _CODE_KINDS and item.sql.strip() and item.original.strip():
            items.append(item)
    return items


def _truncated(text: str) -> str:
    body = text[:_MAX_DEF_CHARS]
    return body + "\n-- …truncated…" if len(text) > _MAX_DEF_CHARS else body


def _context(items: list[PlanItem], target_schema: str) -> str:
    lines = [
        f"Target schema: {target_schema}",
        f"Objects: {len(items)}",
    ]
    for item in items:
        source = item.id.split(":", 1)[1] if ":" in item.id else item.id
        lines += [
            "",
            f"### {item.kind.value.upper()} {source}  ->  {item.name}",
            "",
            "ORIGINAL T-SQL:",
            _truncated(item.original),
            "",
            "TRANSLATED POSTGRESQL:",
            _truncated(item.sql),
        ]
    return "\n".join(lines)


def _extract_json(content: str) -> dict:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            candidate = candidate[start : end + 1]
    # strict=False tolerates raw newlines inside string values, a common model slip.
    return json.loads(candidate, strict=False)


def generate_ai_notes(project: Project, endpoint: str | None = None) -> AiNotes:
    """Ask the Foundation Model what each translated object means for its callers.

    Never raises: a failure comes back as ``success=False`` with the error, so the
    caller can still return the deterministic bundle.
    """
    endpoint = endpoint or FM_ENDPOINT
    items = code_items(project)
    if not items:
        return AiNotes(
            endpoint=endpoint,
            success=False,
            error="This migration has no translated procedures, functions, views or "
                  "triggers, so there is nothing for a model to read.",
        )

    kinds = {
        (item.id.split(":", 1)[1] if ":" in item.id else item.id): item.kind.value.upper()
        for item in items
    }
    try:
        resp = query_chat(
            endpoint,
            messages=[
                ChatMessage(role=ChatMessageRole.SYSTEM, content=_system_prompt()),
                ChatMessage(
                    role=ChatMessageRole.USER,
                    content=_context(items[:_MAX_OBJECTS], project.target_schema),
                ),
            ],
            temperature=0.0,
            max_tokens=_MAX_OUTPUT_TOKENS,
            response_format=_RESPONSE_FORMAT,
        )
        if (resp.choices[0].finish_reason or "").lower() == "length":
            return AiNotes(
                endpoint=endpoint,
                success=False,
                error="The model hit its output-token limit before finishing — re-run, "
                      "or pick an endpoint with a larger output window in Settings.",
            )
        data = _extract_json(chat_text(resp))
        notes = [
            AiObjectNote(
                source=str(n.get("source", "")).strip(),
                object_type=kinds.get(str(n.get("source", "")).strip(), ""),
                call_site=str(n.get("call_site", "")).strip(),
                behaviour=str(n.get("behaviour", "")).strip(),
                watch_out=str(n.get("watch_out", "")).strip(),
            )
            for n in data.get("notes", [])
            if isinstance(n, dict) and str(n.get("source", "")).strip()
        ]
        return AiNotes(
            endpoint=endpoint, notes=notes, objects_total=len(items), success=True
        )
    except Exception as exc:  # fail soft — the deterministic export must still render
        log.exception("App-migration AI notes failed (endpoint=%s)", endpoint)
        return AiNotes(endpoint=endpoint, success=False, error=str(exc))
