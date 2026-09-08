from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
import re
from typing import Any, Protocol

from .models import EssenceMessage, MessageTimeRecord
from .sync_timing import stage
from .normalization import (
    first_value,
    format_timestamp,
    get_message_id,
    get_message_random,
    get_message_sequence,
    needs_message_detail,
    normalize_essence_items,
)


ActionCaller = Callable[..., Awaitable[Any]]


class AstrBotActionContextLike(Protocol):
    bot: Any


class OneBotActionError(RuntimeError):
    def __init__(
        self,
        action: str,
        *,
        status: Any = "",
        retcode: Any = None,
        wording: Any = "",
    ) -> None:
        self.action = _safe_action(action)
        self.status = _safe_fragment(status, 32) or "n/a"
        self.retcode = _safe_fragment(retcode, 32) or "n/a"
        self.wording = _safe_fragment(wording, 120)
        super().__init__(self.public_message)

    @classmethod
    def from_envelope(cls, action: str, envelope: Mapping[str, Any]) -> OneBotActionError:
        return cls(
            action,
            status=envelope.get("status"),
            retcode=envelope.get("retcode"),
            wording=(
                envelope.get("wording")
                or envelope.get("msg")
                or envelope.get("message")
                or ""
            ),
        )

    @property
    def public_message(self) -> str:
        return (
            f"OneBot action={self.action}, status={self.status}, "
            f"retcode={self.retcode}"
        )


def unwrap_action_result(result: Any, *, action: str = "unknown") -> Any:
    if isinstance(result, Mapping) and "data" in result:
        status = str(result.get("status", "")).lower()
        retcode = result.get("retcode")
        if (status and status != "ok") or retcode not in (None, 0):
            raise OneBotActionError.from_envelope(action, result)
        return result["data"]
    return result


class AstrBotEssenceSource:
    async def get_essence_messages(
        self,
        action_context: AstrBotActionContextLike | Any,
        group_id: str,
        *,
        detail_request_limit: int | None = None,
        skip_detail_ids: Iterable[str] = (),
    ) -> list[EssenceMessage]:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            raise ValueError("调用 OneBot Action 时必须提供目标群")

        call_action = _resolve_action_caller(action_context)
        group_parameter: str | int = (
            int(normalized_group_id)
            if normalized_group_id.isdigit()
            else normalized_group_id
        )
        with stage("list"):
            items = await _call_action(
                call_action,
                "get_essence_msg_list",
                group_id=group_parameter,
            )
        if not isinstance(items, list):
            raise OneBotActionError(
                "get_essence_msg_list",
                status="invalid_data",
                wording="data is not a list",
            )

        valid_items: list[Mapping[str, Any]] = []
        details: dict[str, Mapping[str, Any]] = {}
        detail_errors: dict[str, str] = {}
        detail_requested_ids: set[str] = set()
        normalized_detail_limit = _normalize_detail_request_limit(
            detail_request_limit
        )
        skipped_detail_ids = {
            str(value).strip() for value in skip_detail_ids if str(value).strip()
        }
        for item in items:
            if not isinstance(item, Mapping):
                continue
            valid_items.append(item)
            if not needs_message_detail(item):
                continue

            message_id = get_message_id(item)
            if not message_id or message_id in detail_requested_ids:
                continue
            if message_id in skipped_detail_ids:
                continue
            if (
                normalized_detail_limit is not None
                and len(detail_requested_ids) >= normalized_detail_limit
            ):
                continue
            detail_requested_ids.add(message_id)
            try:
                with stage("detail"):
                    detail = await _call_action(
                        call_action,
                        "get_msg",
                        message_id=message_id,
                    )
                if not isinstance(detail, Mapping):
                    raise OneBotActionError(
                        "get_msg",
                        status="invalid_data",
                        wording="data is not an object",
                    )
                details[message_id] = detail
            except OneBotActionError as exc:
                detail_errors[message_id] = exc.public_message

        with stage("normalize"):
            return normalize_essence_items(
                valid_items,
                requested_group_id=normalized_group_id,
                details=details,
                detail_errors=detail_errors,
                detail_requested_ids=detail_requested_ids,
            )

    async def get_group_history_times(
        self,
        action_context: AstrBotActionContextLike | Any,
        group_id: str,
        *,
        limit: int,
    ) -> list[MessageTimeRecord]:
        """读取有界群历史并仅保留身份与时间，不保留正文或发送者。"""
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            raise ValueError("调用 OneBot Action 时必须提供目标群")

        call_action = _resolve_action_caller(action_context)
        group_parameter: str | int = (
            int(normalized_group_id)
            if normalized_group_id.isdigit()
            else normalized_group_id
        )
        history_limit = _normalize_history_limit(limit)
        data = await _call_action(
            call_action,
            "get_group_msg_history",
            group_id=group_parameter,
            count=history_limit,
        )
        if isinstance(data, Mapping):
            items = data.get("messages")
        else:
            items = data
        if not isinstance(items, list):
            raise OneBotActionError(
                "get_group_msg_history",
                status="invalid_data",
                wording="data.messages is not a list",
            )

        records: list[MessageTimeRecord] = []
        for item in items[:history_limit]:
            if not isinstance(item, Mapping):
                continue
            sender_time = format_timestamp(
                first_value(item.get("time"), item.get("sender_time"))
            )
            message_id = get_message_id(item)
            message_seq = get_message_sequence(item)
            if not sender_time or not (message_id or message_seq):
                continue
            records.append(
                MessageTimeRecord(
                    sender_time=sender_time,
                    message_id=message_id,
                    message_seq=message_seq,
                    message_random=get_message_random(item),
                )
            )
        return records


def apply_history_sender_times(
    messages: Iterable[EssenceMessage],
    history: Iterable[MessageTimeRecord],
    *,
    candidate_message_ids: Iterable[str] | None = None,
) -> tuple[list[EssenceMessage], int]:
    """以消息 ID、序号和随机号匹配历史时间，且不复制历史正文。"""
    records = [record for record in history if record.sender_time.strip()]
    by_id = _unique_record_index(records, lambda record: record.message_id)
    by_sequence_random = _unique_record_index(
        records,
        lambda record: (
            f"{record.message_seq}:{record.message_random}"
            if record.message_seq and record.message_random
            else ""
        ),
    )
    by_sequence = _unique_record_index(records, lambda record: record.message_seq)
    allowed_ids = (
        None
        if candidate_message_ids is None
        else {
            str(value).strip()
            for value in candidate_message_ids
            if str(value).strip()
        }
    )

    enriched: list[EssenceMessage] = []
    changed = 0
    for message in messages:
        if message.sender_time.strip():
            enriched.append(message)
            continue
        if allowed_ids is not None and message.message_id not in allowed_ids:
            enriched.append(message)
            continue

        raw_data = dict(message.raw_data or {})
        essence = raw_data.get("essence")
        if not isinstance(essence, Mapping):
            essence = {}
        sequence = get_message_sequence(essence)
        random_value = get_message_random(essence)
        record = by_id.get(message.message_id)
        if record is None and sequence and random_value:
            record = by_sequence_random.get(f"{sequence}:{random_value}")
        if record is None and sequence:
            record = by_sequence.get(sequence)
        if record is None:
            enriched.append(message)
            continue

        raw_data["sender_time_source"] = "group_history"
        enriched.append(
            replace(
                message,
                sender_time=record.sender_time,
                raw_data=raw_data,
            )
        )
        changed += 1
    return enriched, changed


def _unique_record_index(
    records: Iterable[MessageTimeRecord],
    key_builder: Callable[[MessageTimeRecord], str],
) -> dict[str, MessageTimeRecord]:
    index: dict[str, MessageTimeRecord] = {}
    ambiguous: set[str] = set()
    for record in records:
        key = key_builder(record)
        if not key or key in ambiguous:
            continue
        if key in index and index[key] != record:
            index.pop(key, None)
            ambiguous.add(key)
            continue
        index[key] = record
    return index


def _resolve_action_caller(action_context: AstrBotActionContextLike | Any) -> ActionCaller:
    direct_call_action = getattr(action_context, "call_action", None)
    if callable(direct_call_action):
        return direct_call_action

    bot = getattr(action_context, "bot", None)
    api = getattr(bot, "api", None)
    if api is None:
        api = getattr(bot, "_api", None)
    call_action = getattr(api, "call_action", None)
    if not callable(call_action):
        raise OneBotActionError("resolve_api", status="unavailable")
    return call_action


async def _call_action(call_action: ActionCaller, action: str, **params: Any) -> Any:
    try:
        result = await call_action(action=action, **params)
        return unwrap_action_result(result, action=action)
    except OneBotActionError:
        raise
    except Exception as exc:
        raise OneBotActionError(
            action,
            status="exception",
            wording=type(exc).__name__,
        ) from None


def _safe_action(value: Any) -> str:
    action = re.sub(r"[^a-zA-Z0-9_.-]", "", str(value or ""))
    return action[:64] or "unknown"


def _safe_fragment(value: Any, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    text = re.sub(r"https?://\S+", "[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(?:token|cookie|authorization)\s*[:=]\s*\S+",
        "[redacted]",
        text,
    )
    text = re.sub(r"(?<!\d)\d{5,}(?!\d)", "[id]", text)
    return text[:limit]


def _normalize_detail_request_limit(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _normalize_history_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 100
    return max(1, min(parsed, 500))
