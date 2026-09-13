"""Safe normalization and validation of HKTE notice reply forms."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from .api import _as_date_value, _clean_text

TYPES = {
    "ReadOnly": "acknowledgement",
    "Choice": "single_choice",
    "Choices": "multiple_choice",
    "Comment": "text",
    "Feedback": "text",
    "ItemQuantities": "quantities",
    "ItemSet": "quantities",
}


class FormError(Exception):
    """Safe validation code for an unsupported or invalid form."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _flag(value: Any) -> bool:
    return value is True or value == 1 or value == "1"


def _deadline(value: Any) -> str | None:
    parsed = _as_date_value(value)
    if isinstance(parsed, datetime):
        return parsed.isoformat()
    if isinstance(parsed, date):
        return parsed.isoformat()
    return None


def _array(raw: Mapping[str, Any], key: str, reasons: list[str]) -> list[Any]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        reasons.append("invalid_form")
        return []
    return value


def build_form(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a raw GetAllNotices record into a bounded public form."""
    reasons: list[str] = []
    questions: list[dict[str, Any]] = []
    sections: dict[str, Mapping[str, Any]] = {}
    for section in _array(raw, "sectionsData", reasons):
        if not isinstance(section, Mapping) or str(section.get("id")) in sections:
            reasons.append("invalid_form")
            continue
        sections[str(section.get("id"))] = section

    def add(
        node: Mapping[str, Any],
        key: str,
        condition: dict[str, Any] | None = None,
        stack: tuple[str, ...] = (),
    ) -> None:
        if len(stack) > 12 or len(questions) >= 200:
            reasons.append("invalid_form")
            return
        kind = node.get("qtype") or node.get("ntype")
        kind = kind if isinstance(kind, str) else None
        options_raw = _array(node, "options", reasons)
        options = [
            {"value": index, "label": _clean_text(value, 4000)}
            for index, value in enumerate(options_raw)
            if isinstance(value, str)
        ]
        if len(options) != len(options_raw):
            reasons.append("invalid_form")
        ranges: list[dict[str, int] | None] = []
        for bounds in _array(node, "numrange", reasons):
            if bounds is None:
                ranges.append(None)
            elif isinstance(bounds, Mapping) and all(
                type(bounds.get(name, -1)) is int and bounds.get(name, -1) >= -1
                for name in ("min", "max")
            ):
                low, high = int(bounds.get("min", -1)), int(bounds.get("max", -1))
                if high >= 0 and low > high:
                    reasons.append("invalid_form")
                ranges.append({"min": low, "max": high})
            else:
                reasons.append("invalid_form")
        if len(ranges) > len(options):
            reasons.append("invalid_form")
        question = {
            "id": key,
            "type": TYPES.get(kind or "", "unsupported"),
            "upstream_type": kind,
            "label": _clean_text(
                node.get("content") or node.get("body") or raw.get("title"), 20000
            ),
            "required": not _flag(node.get("optional")) and kind != "ReadOnly",
            "options": options,
            "ranges": ranges,
            "when": condition,
        }
        questions.append(question)
        if kind not in TYPES or (
            kind in {"Choice", "Choices", "ItemQuantities", "ItemSet"} and not options
        ):
            reasons.append("unsupported_form")
        if _flag(node.get("payment")) or _flag(node.get("has_payment")):
            reasons.append("payment_form")
        for price in _array(node, "prices", reasons):
            if str(price).strip() not in {"", "0", "0.0", "0.00"}:
                reasons.append("payment_form")
        branches = _array(node, "subsections" if "qtype" in node else "sections", reasons)
        if "qtype" in node and any(value not in (None, "", 0, "0") for value in branches):
            reasons.append("nested_form")
        for index, section_id in enumerate(branches):
            if section_id in (None, "", 0, "0"):
                continue
            section = sections.get(str(section_id))
            if (
                not section
                or not isinstance(section.get("question"), list)
                or str(section_id) in stack
            ):
                reasons.append("invalid_form")
                continue
            if kind not in {
                "Choice",
                "Choices",
                "Comment",
                "Feedback",
                "ItemQuantities",
                "ItemSet",
            }:
                reasons.append("unsupported_form")
                continue
            if index >= (1 if kind in {"Comment", "Feedback"} else len(options)):
                reasons.append("invalid_form")
                continue
            for child in section["question"]:
                if (
                    not isinstance(child, Mapping)
                    or not str(child.get("id", "")).isdigit()
                    or int(child.get("id", 0)) <= 0
                ):
                    reasons.append("invalid_form")
                    continue
                add(
                    child,
                    f"question:{child['id']}",
                    {"question_id": key, "option_index": index},
                    (*stack, str(section_id)),
                )

    add(raw, "main")
    for index, child in enumerate(_array(raw, "sub_notices", reasons)):
        if isinstance(child, Mapping):
            add(child, f"sub:{index}")
        else:
            reasons.append("invalid_form")

    if len({question["id"] for question in questions}) != len(questions):
        reasons.append("invalid_form")

    revision = {
        key: raw.get(key)
        for key in (
            "title",
            "body",
            "introduction",
            "ntype",
            "options",
            "prices",
            "numrange",
            "optional",
            "sections",
            "sectionsData",
            "sub_notices",
            "version",
            "deadline",
            "allow_reply_after_deadline",
            "has_payment",
            "subsidy_status",
        )
    }
    revision["attachments"] = [
        {
            key: attachment.get(key)
            for key in (
                "itemId",
                "itemid",
                "attachment_id",
                "id",
                "name",
                "filename",
                "type",
                "mime_type",
            )
        }
        for attachment in _array(raw, "attachments", reasons)
        if isinstance(attachment, Mapping)
    ]
    version = hashlib.sha256(
        json.dumps(
            revision, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()
    deadline = _deadline(raw.get("deadline"))
    expired = False
    if raw.get("deadline") is not None and not deadline:
        reasons.append("invalid_deadline")
    if deadline:
        try:
            moment = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            expired = moment < datetime.now(UTC) and not _flag(
                raw.get("allow_reply_after_deadline")
            )
        except ValueError:
            reasons.append("invalid_deadline")
    unique_reasons = list(dict.fromkeys(reasons))
    replied = _flag(raw.get("replied"))
    return {
        "title": _clean_text(raw.get("title"), 1000),
        "introduction": _clean_text(raw.get("introduction"), 20000),
        "form_version": version,
        "deadline": deadline,
        "unread": _flag(raw.get("unread")),
        "replied": replied,
        "supported": not unique_reasons,
        "can_sign": not unique_reasons and not expired and not replied,
        "blocked_reasons": unique_reasons
        + (["expired"] if expired else [])
        + (["already_replied"] if replied else []),
        "questions": questions[:200],
        "submission_warning": "回覆提交後不能更改，請確認答案無誤。",
        "can_edit_after_submission": False,
        "auto_sign_read_only": _flag(raw.get("auto_sign_read_only")),
    }


def validate_reply(
    raw: Mapping[str, Any], form_version: str, answers: Any, comment: Any
) -> dict[str, Any]:
    """Validate UI answers and encode the official SignNotice payload."""
    form = build_form(raw)
    if form["form_version"] != form_version:
        raise FormError("form_changed")
    if not form["can_sign"] or not form["supported"]:
        raise FormError("cannot_sign")
    if not isinstance(answers, Mapping) or not isinstance(comment, str) or len(comment) > 10000:
        raise FormError("invalid_answers")
    by_id = {question["id"]: question for question in form["questions"]}
    values: dict[str, Any] = {}
    active: set[str] = set()
    for question in form["questions"]:
        condition = question["when"]
        if condition:
            parent = by_id.get(condition["question_id"])
            if parent is None or parent["id"] not in active:
                continue
            parent_value = values.get(parent["id"])
            index = condition["option_index"]
            enabled = (
                bool(parent_value.strip())
                if parent["type"] == "text" and isinstance(parent_value, str)
                else index in parent_value
                if isinstance(parent_value, list) and parent["type"] != "quantities"
                else isinstance(parent_value, list)
                and index < len(parent_value)
                and parent_value[index] > 0
            )
            if not enabled:
                continue
        active.add(question["id"])
        supplied = question["id"] in answers
        value = answers.get(question["id"])
        kind = question["type"]
        if kind == "acknowledgement":
            if supplied and value != "":
                raise FormError("invalid_answers")
            value = ""
        elif kind == "text":
            if not supplied and not question["required"]:
                value = ""
            if (
                not isinstance(value, str)
                or len(value) > 10000
                or (question["required"] and not value.strip())
            ):
                raise FormError("invalid_answers")
        else:
            if not supplied and not question["required"]:
                value = [0] * len(question["options"]) if kind == "quantities" else []
            if not isinstance(value, list) or any(
                type(item) is not int or item < 0 or item > 100000 for item in value
            ):
                raise FormError("invalid_answers")
            if kind in {"single_choice", "multiple_choice"}:
                if (
                    (kind == "single_choice" and len(value) > 1)
                    or len(set(value)) != len(value)
                    or any(item >= len(question["options"]) for item in value)
                    or (question["required"] and not value)
                ):
                    raise FormError("invalid_answers")
                if kind == "single_choice" and not value:
                    value = [-1] if question["id"] == "main" else []
            elif kind == "quantities":
                if len(value) != len(question["options"]) or (
                    question["required"] and not any(value)
                ):
                    raise FormError("invalid_answers")
                for index, bounds in enumerate(question["ranges"]):
                    if (
                        bounds
                        and index < len(value)
                        and (
                            (bounds["min"] >= 0 and value[index] < bounds["min"])
                            or (bounds["max"] >= 0 and value[index] > bounds["max"])
                        )
                    ):
                        raise FormError("invalid_answers")
            else:
                raise FormError("unsupported_form")
        values[question["id"]] = value
    if set(answers) - active:
        raise FormError("invalid_answers")
    payload: dict[str, Any] = {
        "reply": values["main"],
        "amount": 0,
        "amount_without_extra_subsidy": 0,
    }
    if comment:
        payload["comment"] = comment
    subsidy = raw.get("subsidy_status", -1)
    if subsidy not in (None, -1):
        payload["subsidy_status"] = subsidy
    subs = [
        {"reply": None if values[f"sub:{index}"] == [] else values[f"sub:{index}"]}
        for index in range(len(raw.get("sub_notices") or []))
        if f"sub:{index}" in values
    ]
    if subs:
        payload["sub_notices"] = subs
    sections = [
        {"qid": int(key.split(":", 1)[1]), "reply": None if value == [] else value}
        for key, value in values.items()
        if key.startswith("question:")
    ]
    if sections:
        payload["section_reply"] = sections
    return payload
