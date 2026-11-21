"""Optional source-grounded quote IDs for smaller DeepSeek matching replies.

The model selects an existing quote ID. Python restores its exact text and
passes it to the unchanged source/engineering validators. Unknown IDs never
become evidence. This is a deliberately opt-in experiment.
"""
from __future__ import annotations

import re
from math import isfinite
from typing import Any

from app.requirements.tools import _evidence_fragments, tool_validate_quotes

_MAX_QUOTE_CHARS = 450
_MAX_CHOICES = 8


def quote_choices(
    clause: Any, prefix: str, against: str, *, limit: int = _MAX_CHOICES,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Return deterministic, verbatim, single-fragment spans and their IDs."""
    sources = getattr(clause, "sources", ()) or ()
    if sources:
        raw_sources = [
            s.get("text", "") if isinstance(s, dict) else getattr(s, "text", "")
            for s in sources
        ]
    else:
        raw_sources = [getattr(clause, "fragment_text", None) or clause.text]

    spans: list[str] = []
    seen: set[str] = set()
    for raw in raw_sources:
        for part in _evidence_fragments(str(raw), _MAX_QUOTE_CHARS):
            if not part or part in seen:
                continue
            valid = tool_validate_quotes(clause.text, part, clause.text, part)
            if not (valid["is_valid"] and valid["znp_quote_atomic"]):
                continue
            seen.add(part)
            spans.append(part)

    # Never silently omit quote options: an omitted piece might be the only
    # evidence needed to disprove an apparent positive match.
    if len(spans) > limit or not spans:
        return [], {}

    # Rank short evidence spans but do not alter their contents.
    # Never infer evidence from a title/parent text absent from the source.
    query_tokens = set(re.findall(r"\w+", against.casefold()))
    query_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", against))
    def rank(part: str) -> tuple[int, int]:
        tokens = set(re.findall(r"\w+", part.casefold()))
        numbers = set(re.findall(r"\d+(?:[.,]\d+)?", part))
        return (len(query_numbers & numbers) * 4 + len(query_tokens & tokens), -len(part))

    top = sorted(enumerate(spans), key=lambda row: (-rank(row[1])[0], row[0]))
    selected = [spans[index] for index, _ in top[:limit]]
    entries = [{"id": f"{prefix}{i}", "text": span} for i, span in enumerate(selected)]
    return entries, {entry["id"]: entry["text"] for entry in entries}


def restore_link_quotes(
    reply: Any,
    *,
    tu_maps: dict[str, dict[str, str]],
    znp_maps: dict[str, dict[str, str]],
    mode: str,
) -> Any:
    """Hydrate candidate-selected IDs; existing _validated_link stays authoritative."""
    if not isinstance(reply, dict) or not isinstance(reply.get("links"), list):
        return reply
    restored = {**reply, "links": []}
    for item in reply["links"]:
        if not isinstance(item, dict):
            restored["links"].append(item)
            continue
        proposal = dict(item)
        rid = str(item.get("requirement_id") or "")
        aid = str(item.get("atom_id") or "")
        if mode == "matching":
            tu_map = tu_maps.get(rid, {})
            znp_map = znp_maps.get("_current", {})
        else:
            tu_map = tu_maps.get("_current", {})
            znp_map = znp_maps.get(aid, {})
        proposal.pop("tu_quote", None)
        proposal.pop("znp_quote", None)
        tu_id = item.get("tu_evidence_id")
        znp_id = item.get("znp_evidence_id")
        if isinstance(tu_id, str) and tu_id in tu_map:
            proposal["tu_quote"] = tu_map[tu_id]
        if isinstance(znp_id, str) and znp_id in znp_map:
            proposal["znp_quote"] = znp_map[znp_id]
        restored["links"].append(proposal)
    return restored


# JSON Schema for optional DeepSeek strict function calls (beta).
# All object fields are required and additional properties disallowed.
_LINK_FIELDS = {
    "relation": {"type": "string", "enum": ["supports", "partial", "conflicts"]},
    "tu_evidence_id": {"type": "string"},
    "znp_evidence_id": {"type": "string"},
    "confidence": {"type": "number"},
    "reasoning": {"type": "string"},
}


def link_schema(*, targeted: bool) -> dict[str, Any]:
    props = {
        ("atom_id" if targeted else "requirement_id"): {"type": "string"},
        **_LINK_FIELDS,
    }
    root = {
        "links": {
            "type": "array", "items": {
                "type": "object", "properties": props,
                "required": list(props), "additionalProperties": False,
            },
        },
    }
    if not targeted:
        root["search_more"] = {"type": "boolean"}
    return {
        "type": "object",
        "properties": root,
        "required": list(root),
        "additionalProperties": False,
    }


def validate_strict_tool_arguments(value: Any, schema: dict[str, Any]) -> bool:
    """Validate the small, known subset of JSON Schema we send to DeepSeek.

    A forced tool call with `strict: true` is a provider request, not proof
    that the received `arguments` comply. Reject incomplete objects, extra
    fields, wrong nested types and nonfinite confidence values locally.
    """
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if any(field not in value for field in required):
            return False
        if schema.get("additionalProperties") is False and (
            set(value) - set(properties)
        ):
            return False
        return all(
            name in properties
            and validate_strict_tool_arguments(field_value, properties[name])
            for name, field_value in value.items()
        )
    if expected == "array":
        return isinstance(value, list) and all(
            validate_strict_tool_arguments(item, schema["items"])
            for item in value
        )
    if expected == "string":
        return isinstance(value, str) and (
            "enum" not in schema or value in schema["enum"]
        )
    if expected == "number":
        return (isinstance(value, (int, float))
                and not isinstance(value, bool)
                and isfinite(value))
    if expected == "boolean":
        return isinstance(value, bool)
    return False
