"""Basecamp event adapter for Hermes Agent.

The adapter uses the official ``basecamp`` CLI with a named authenticated
profile. It polls Basecamp's Hey! readings and assignment report, emits only
explicit Pings, verified mentions, new assignments and opted-in comments on
subscribed work threads, persists cursor state, and sends replies through the
same Basecamp identity.

The initial T40 deployment is deliberately fail-closed: approved project IDs,
Basecamp person IDs and the Hermes person ID must all be configured.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime, timezone
import html
import json
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any, Dict, Iterable, List, Optional, Sequence

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_PROFILE = "hermes"
DEFAULT_POLL_INTERVAL = 8.0
DEFAULT_COMMAND_TIMEOUT = 45.0
DEFAULT_STATE_LIMIT = 2000
TARGET_RE = re.compile(r"^(ping|chat|recording):(\d+):(\d+)$")
BUCKET_RE = re.compile(r"/(?:buckets|circles)/(\d+)")
RECORDING_RE = re.compile(
    r"/(?:todos|messages|cards|recordings|documents|uploads|questions|schedule_entries)/(\d+)"
)
CHAT_RE = re.compile(r"/chats/(\d+)")
TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]+)\]\(([^)]+)\)")
SPACE_RE = re.compile(r"\s+")


class BasecampCliError(RuntimeError):
    """The Basecamp CLI failed or returned a malformed response."""


def _csv_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value).split(",")
    return {str(item).strip() for item in values if str(item).strip()}


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _plain_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", html.unescape(text)).strip()


def _ping_plain_text(value: Any) -> str:
    """Render model Markdown as one natural Basecamp Ping paragraph.

    Structured multi-paragraph chat lines are displayed by Basecamp with a
    long-message rail. Direct Pings should read like chat, so strip lightweight
    Markdown and flatten whitespace before sending.
    """
    text = html.unescape(str(value or ""))
    text = BR_RE.sub("\n", text)
    text = MARKDOWN_LINK_RE.sub(lambda match: f"{match.group(1)} ({match.group(2)})", text)
    text = re.sub(r"```(?:[A-Za-z0-9_+-]+)?\s*|```", "", text)
    text = re.sub(r"(?m)^\s{0,3}(?:[-*+] |\d+[.)] )", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    text = re.sub(r"(\*\*|__|~~|`)", "", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", text)
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def _url_id(pattern: re.Pattern[str], value: Any) -> Optional[str]:
    match = pattern.search(str(value or ""))
    return match.group(1) if match else None


def _readable_recording_id(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    match = re.search(r"/(\d+)$", decoded)
    return match.group(1) if match else None


def _parse_timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return datetime.now(timezone.utc)


def _reading_key(item: dict) -> str:
    """Stable cursor key for one unread revision.

    Basecamp reuses a Hey! reading ID as later lines arrive in the same Ping,
    but advances ``unread_at``. Include both so follow-up Pings are not lost.
    """
    return f"{item.get('id') or ''}:{item.get('unread_at') or item.get('created_at') or ''}"


def _trim(values: Iterable[str], limit: int) -> list[str]:
    items = list(dict.fromkeys(str(v) for v in values if str(v)))
    return items[-max(1, limit) :]


def _data_from_envelope(value: Any) -> Any:
    if isinstance(value, dict) and "ok" in value:
        if not value.get("ok"):
            raise BasecampCliError(str(value.get("error") or "Basecamp CLI request failed"))
        return value.get("data")
    return value


def _flatten_assignments(value: Any) -> list[dict]:
    if isinstance(value, list):
        roots = value
    elif isinstance(value, dict):
        roots = list(value.get("priorities") or []) + list(value.get("non_priorities") or [])
    else:
        return []
    result: list[dict] = []
    stack = list(roots)
    while stack:
        item = stack.pop(0)
        if not isinstance(item, dict):
            continue
        result.append(item)
        children = item.get("children")
        if isinstance(children, list):
            stack[0:0] = children
    return result


def check_requirements() -> bool:
    return bool(_basecamp_binary())


def validate_config(config: PlatformConfig) -> bool:
    extra = config.extra or {}
    return bool(
        (extra.get("account_id") or os.getenv("BASECAMP_ACCOUNT_ID"))
        and (extra.get("person_id") or os.getenv("BASECAMP_PERSON_ID"))
        and _csv_set(extra.get("project_ids") or os.getenv("BASECAMP_PROJECT_IDS"))
    )


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config) and check_requirements()


def _basecamp_binary() -> Optional[str]:
    configured = os.getenv("BASECAMP_CLI_PATH", "").strip()
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("basecamp")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "basecamp"
    return str(fallback) if fallback.is_file() else None


class BasecampAdapter(BasePlatformAdapter):
    """Long-polling Basecamp platform adapter backed by the official CLI."""

    supports_code_blocks = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("basecamp"))
        extra = config.extra or {}
        self._binary = str(extra.get("cli_path") or _basecamp_binary() or "")
        self._profile = str(
            extra.get("profile") or os.getenv("BASECAMP_CLI_PROFILE") or DEFAULT_PROFILE
        ).strip()
        self._account_id = str(
            extra.get("account_id") or os.getenv("BASECAMP_ACCOUNT_ID") or ""
        ).strip()
        self._person_id = str(
            extra.get("person_id") or os.getenv("BASECAMP_PERSON_ID") or ""
        ).strip()
        self._project_ids = _csv_set(
            extra.get("project_ids") or os.getenv("BASECAMP_PROJECT_IDS")
        )
        self._allowed_users = _csv_set(
            extra.get("allowed_users") or os.getenv("BASECAMP_ALLOWED_USERS")
        )
        self._poll_interval = max(
            2.0, float(extra.get("poll_interval", DEFAULT_POLL_INTERVAL))
        )
        self._command_timeout = max(
            5.0, float(extra.get("command_timeout", DEFAULT_COMMAND_TIMEOUT))
        )
        self._state_limit = max(100, int(extra.get("state_limit", DEFAULT_STATE_LIMIT)))
        default_state = get_hermes_home() / "state" / "basecamp" / f"{self._profile}.json"
        self._state_path = Path(str(extra.get("state_path") or default_state)).expanduser()
        self._mark_read = _bool(extra.get("mark_read"), True)
        self._bootstrap_silently = _bool(extra.get("bootstrap_silently"), True)
        self._follow_subscribed_comments = _bool(
            extra.get("follow_subscribed_comments"), False
        )
        self._poll_task: Optional[asyncio.Task] = None
        self._poll_lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            "version": 1,
            "bootstrapped": False,
            "seen_readings": [],
            "assignment_ids": [],
            "seen_events": [],
        }
        self._identity: dict[str, Any] = {}
        self._attachable_sgid = str(extra.get("attachable_sgid") or "")

    @property
    def enforces_own_access_policy(self) -> bool:
        """The adapter fail-closes on its non-empty Basecamp person allowlist."""
        return True

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._binary:
            self._set_fatal_error(
                "basecamp_cli_missing", "Official Basecamp CLI not found", retryable=False
            )
            return False
        if not validate_config(self.config):
            self._set_fatal_error(
                "basecamp_config_invalid",
                "Basecamp account_id, person_id and project_ids are required",
                retryable=False,
            )
            return False
        try:
            self._load_state()
            await self._validate_identity_and_access()
            if not self._state.get("bootstrapped"):
                await self.poll_once(dispatch=not self._bootstrap_silently)
                self._state["bootstrapped"] = True
                self._save_state()
            self._mark_connected()
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info(
                "[basecamp] Connected profile=%s account=%s projects=%s",
                self._profile,
                self._account_id,
                ",".join(sorted(self._project_ids)),
            )
            return True
        except Exception as exc:
            logger.error("[basecamp] Connection failed: %s", exc, exc_info=True)
            self._set_fatal_error("basecamp_connect_failed", str(exc), retryable=True)
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        self._save_state()
        logger.info("[basecamp] Disconnected")

    async def _validate_identity_and_access(self) -> None:
        payload = await self._cli_json("me", "--json")
        data = _data_from_envelope(payload) or {}
        identity = data.get("identity") if isinstance(data, dict) else None
        if not isinstance(identity, dict):
            raise BasecampCliError("Basecamp identity response was missing identity data")
        identity_email = str(identity.get("email_address") or "").strip().lower()
        person_payload = await self._cli_json(
            "--account", self._account_id, "people", "show", self._person_id, "--quiet"
        )
        person = _data_from_envelope(person_payload)
        if not isinstance(person, dict):
            raise BasecampCliError("Basecamp person response was missing person data")
        actual = str(person.get("id") or "")
        person_email = str(person.get("email_address") or "").strip().lower()
        if actual != self._person_id:
            raise BasecampCliError(
                f"Basecamp profile {self._profile!r} resolved person {actual or 'unknown'}, expected {self._person_id}"
            )
        if identity_email and person_email and identity_email != person_email:
            raise BasecampCliError(
                f"Basecamp profile email {identity_email!r} does not match person email {person_email!r}"
            )
        self._identity = person
        self._attachable_sgid = self._attachable_sgid or str(person.get("attachable_sgid") or "")

        projects = await self._cli_json(
            "--account", self._account_id, "projects", "list", "--json"
        )
        project_data = _data_from_envelope(projects)
        if isinstance(project_data, dict):
            project_data = project_data.get("projects") or project_data.get("items") or []
        accessible = {
            str(item.get("id"))
            for item in (project_data or [])
            if isinstance(item, dict) and item.get("id") is not None
        }
        missing = self._project_ids - accessible
        if missing:
            raise BasecampCliError(
                f"Basecamp profile lacks approved project access: {','.join(sorted(missing))}"
            )

    async def _poll_loop(self) -> None:
        backoff = self._poll_interval
        while self._running:
            try:
                await self.poll_once(dispatch=True)
                backoff = self._poll_interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[basecamp] Poll failed: %s", exc, exc_info=True)
                backoff = min(max(self._poll_interval, backoff * 2), 120.0)
            await asyncio.sleep(backoff)

    async def poll_once(self, *, dispatch: bool = True) -> int:
        """Poll readings and assignments once; return dispatched event count."""
        async with self._poll_lock:
            reading_payload, assignment_payload = await asyncio.gather(
                self._cli_json(
                    "--account", self._account_id, "api", "get", "/my/readings.json", "--quiet"
                ),
                self._cli_json(
                    "--account", self._account_id, "api", "get", "/my/assignments.json", "--quiet"
                ),
            )
            readings_data = _data_from_envelope(reading_payload) or {}
            unreads = readings_data.get("unreads") if isinstance(readings_data, dict) else []
            if not isinstance(unreads, list):
                unreads = []
            assignments = _flatten_assignments(_data_from_envelope(assignment_payload))

            seen_readings = set(map(str, self._state.get("seen_readings") or []))
            seen_events = set(map(str, self._state.get("seen_events") or []))
            previous_assignments = set(map(str, self._state.get("assignment_ids") or []))
            current_assignments = {
                str(item.get("id")) for item in assignments if item.get("id") is not None
            }

            fresh_readings = [
                item
                for item in unreads
                if isinstance(item, dict) and _reading_key(item) not in seen_readings
            ]
            new_assignments = [
                item
                for item in assignments
                if str(item.get("id") or "") not in previous_assignments
            ]

            # Persist the observed frontier before dispatch. This provides
            # at-most-once delivery across process crashes and prevents loops.
            seen_readings.update(_reading_key(item) for item in fresh_readings if item.get("id"))
            self._state["seen_readings"] = _trim(seen_readings, self._state_limit)
            self._state["assignment_ids"] = _trim(current_assignments, self._state_limit)
            self._save_state()

            if not dispatch:
                return 0

            dispatched = 0
            processed_reading_ids: list[str] = []
            assignment_reading_ids: set[str] = set()

            for reading in sorted(
                fresh_readings, key=lambda item: str(item.get("unread_at") or item.get("created_at") or "")
            ):
                event = await self._event_from_reading(reading)
                reading_id = str(reading.get("id") or "")
                if reading_id:
                    processed_reading_ids.append(reading_id)
                if str(reading.get("type") or "").lower() == "assignment":
                    record_id = _url_id(RECORDING_RE, reading.get("app_url"))
                    if record_id:
                        assignment_reading_ids.add(record_id)
                if not event:
                    continue
                key = str(event.metadata.get("basecamp_event_key") or event.message_id or "")
                if key and key in seen_events:
                    continue
                if key:
                    seen_events.add(key)
                    self._state["seen_events"] = _trim(seen_events, self._state_limit)
                    self._save_state()
                await self.handle_message(event)
                dispatched += 1

            # Assignment polling is a safety net when Hey! did not produce a
            # reading. The first bootstrap records the current set and emits none.
            for assignment in new_assignments:
                record_id = str(assignment.get("id") or "")
                if not record_id or record_id in assignment_reading_ids:
                    continue
                event = self._event_from_assignment(assignment)
                if not event:
                    continue
                key = str(event.metadata.get("basecamp_event_key") or event.message_id or "")
                if key and key in seen_events:
                    continue
                if key:
                    seen_events.add(key)
                    self._state["seen_events"] = _trim(seen_events, self._state_limit)
                    self._save_state()
                await self.handle_message(event)
                dispatched += 1

            if self._mark_read and processed_reading_ids:
                try:
                    await self._cli_json(
                        "--account",
                        self._account_id,
                        "notifications",
                        "read",
                        *dict.fromkeys(processed_reading_ids),
                        "--json",
                    )
                except Exception as exc:
                    logger.warning("[basecamp] Could not mark readings read: %s", exc)

            logger.debug(
                "[basecamp] Poll complete fresh_readings=%d new_assignments=%d dispatched=%d",
                len(fresh_readings),
                len(new_assignments),
                dispatched,
            )
            return dispatched

    async def _event_from_reading(self, reading: dict) -> Optional[MessageEvent]:
        creator = reading.get("creator") or {}
        creator_id = str(creator.get("id") or "")
        if not creator_id or creator_id == self._person_id or not self._user_allowed(creator_id):
            return None

        section = str(reading.get("section") or "").lower()
        reading_type = str(reading.get("type") or "").lower()
        app_url = str(reading.get("app_url") or "")
        bucket_id = _url_id(BUCKET_RE, app_url)
        reading_id = str(reading.get("id") or "")
        timestamp = _parse_timestamp(reading.get("unread_at") or reading.get("created_at"))

        if section == "pings":
            transcript_id = _readable_recording_id(reading.get("readable_identifier"))
            if not bucket_id or not transcript_id:
                return None
            participants = reading.get("participants") or []
            chat_type = "group" if isinstance(participants, list) and len(participants) > 1 else "dm"
            target = f"ping:{bucket_id}:{transcript_id}"
            text = _plain_text(reading.get("content_excerpt") or reading.get("title"))
            if not text:
                return None
            return self._build_event(
                target=target,
                chat_name=str(reading.get("bucket_name") or "Basecamp Ping"),
                chat_type=chat_type,
                creator=creator,
                text=text,
                message_id=reading_id,
                timestamp=timestamp,
                raw=reading,
                event_key=f"reading:{_reading_key(reading)}",
                trigger="ping",
                bucket_id=bucket_id,
                recording_id=transcript_id,
            )

        if not bucket_id or bucket_id not in self._project_ids:
            return None

        if reading_type == "assignment" or str(reading.get("title") or "").lower().startswith("assigned you"):
            recording_id = _url_id(RECORDING_RE, app_url)
            if not recording_id:
                return None
            text = _plain_text(reading.get("content_excerpt") or reading.get("title"))
            target = f"recording:{bucket_id}:{recording_id}"
            return self._build_event(
                target=target,
                chat_name=str(reading.get("bucket_name") or "Basecamp assignment"),
                chat_type="group",
                creator=creator,
                text=f"Basecamp assignment: {text}",
                message_id=reading_id,
                timestamp=timestamp,
                raw=reading,
                event_key=f"assign:{recording_id}:{reading.get('unread_at') or reading_id}",
                trigger="assignment",
                bucket_id=bucket_id,
                recording_id=recording_id,
            )

        # Basecamp puts verified mentions on messages, to-dos, cards and other
        # recordings in the Inbox section. The app URL points to the parent
        # recording, which is where a reply must be posted.
        if reading_type == "mention":
            recording_id = _url_id(RECORDING_RE, app_url)
            if not recording_id:
                return None
            text = _plain_text(reading.get("content_excerpt") or reading.get("title"))
            if not text:
                return None
            return self._build_event(
                target=f"recording:{bucket_id}:{recording_id}",
                chat_name=str(reading.get("bucket_name") or "Basecamp mention"),
                chat_type="group",
                creator=creator,
                text=text,
                message_id=reading_id,
                timestamp=timestamp,
                raw=reading,
                event_key=f"mention:{_reading_key(reading)}",
                trigger="mention",
                bucket_id=bucket_id,
                recording_id=recording_id,
            )

        # Once Hermes is subscribed to a work item, comments on that item are
        # the Basecamp equivalent of replies in an existing thread. This is
        # opt-in because broad subscription activity can otherwise be noisy.
        if (
            reading_type == "comment"
            and self._follow_subscribed_comments
            and reading.get("subscribed") is True
        ):
            recording_id = _url_id(RECORDING_RE, app_url)
            if not recording_id:
                return None
            text = _plain_text(reading.get("content_excerpt") or reading.get("title"))
            if not text:
                return None
            return self._build_event(
                target=f"recording:{bucket_id}:{recording_id}",
                chat_name=str(reading.get("bucket_name") or "Basecamp thread"),
                chat_type="group",
                creator=creator,
                text=text,
                message_id=reading_id,
                timestamp=timestamp,
                raw=reading,
                event_key=f"comment:{_reading_key(reading)}",
                trigger="subscribed_comment",
                bucket_id=bucket_id,
                recording_id=recording_id,
            )

        if section in {"chats", "mentions"}:
            transcript_id = _url_id(CHAT_RE, app_url)
            if not transcript_id:
                return None
            line = await self._resolve_mention_line(reading, bucket_id, transcript_id)
            if not line:
                return None
            line_creator = line.get("creator") or creator
            line_creator_id = str(line_creator.get("id") or "")
            if line_creator_id == self._person_id or not self._user_allowed(line_creator_id):
                return None
            line_id = str(line.get("id") or reading_id)
            text = _plain_text(line.get("content") or reading.get("content_excerpt"))
            target = f"chat:{bucket_id}:{transcript_id}"
            return self._build_event(
                target=target,
                chat_name=str(reading.get("bucket_name") or "Basecamp Campfire"),
                chat_type="group",
                creator=line_creator,
                text=text,
                message_id=line_id,
                timestamp=_parse_timestamp(line.get("created_at") or reading.get("unread_at")),
                raw={"reading": reading, "line": line},
                event_key=f"line:{line_id}",
                trigger="mention",
                bucket_id=bucket_id,
                recording_id=transcript_id,
            )

        return None

    async def _resolve_mention_line(
        self, reading: dict, bucket_id: str, transcript_id: str
    ) -> Optional[dict]:
        payload = await self._cli_json(
            "--account",
            self._account_id,
            "chat",
            "messages",
            "--in",
            bucket_id,
            "--room",
            transcript_id,
            "--quiet",
        )
        lines = _data_from_envelope(payload)
        if not isinstance(lines, list):
            return None
        creator_id = str((reading.get("creator") or {}).get("id") or "")
        excerpt = _plain_text(reading.get("content_excerpt"))
        candidates: list[dict] = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            content = str(line.get("content") or "")
            line_creator = str((line.get("creator") or {}).get("id") or "")
            has_structured_mention = bool(
                self._attachable_sgid and self._attachable_sgid in html.unescape(content)
            )
            # ``section=mentions`` is Basecamp's own verified signal. ``chats``
            # needs the structured mention attachment to avoid waking on every
            # subscribed Campfire line.
            if not has_structured_mention and str(reading.get("section") or "").lower() != "mentions":
                continue
            if creator_id and line_creator != creator_id:
                continue
            plain = _plain_text(content)
            if excerpt and excerpt not in plain and plain not in excerpt:
                continue
            candidates.append(line)
        if not candidates:
            return None
        return max(candidates, key=lambda item: str(item.get("created_at") or ""))

    def _event_from_assignment(self, assignment: dict) -> Optional[MessageEvent]:
        bucket = assignment.get("bucket") or {}
        bucket_id = str(bucket.get("id") or _url_id(BUCKET_RE, assignment.get("app_url")) or "")
        recording_id = str(assignment.get("id") or "")
        if not bucket_id or bucket_id not in self._project_ids or not recording_id:
            return None
        creator = assignment.get("creator") or {}
        creator_id = str(creator.get("id") or "")
        if not creator_id or creator_id == self._person_id or not self._user_allowed(creator_id):
            return None
        assignees = {str(item.get("id")) for item in assignment.get("assignees") or [] if isinstance(item, dict)}
        if self._person_id not in assignees:
            return None
        updated = assignment.get("updated_at") or assignment.get("created_at") or ""
        text = _plain_text(assignment.get("content") or assignment.get("title"))
        return self._build_event(
            target=f"recording:{bucket_id}:{recording_id}",
            chat_name=str(bucket.get("name") or "Basecamp assignment"),
            chat_type="group",
            creator=creator,
            text=f"Basecamp assignment: {text}",
            message_id=recording_id,
            timestamp=_parse_timestamp(updated),
            raw=assignment,
            event_key=f"assign:{recording_id}:{updated}",
            trigger="assignment",
            bucket_id=bucket_id,
            recording_id=recording_id,
        )

    def _build_event(
        self,
        *,
        target: str,
        chat_name: str,
        chat_type: str,
        creator: dict,
        text: str,
        message_id: str,
        timestamp: datetime,
        raw: Any,
        event_key: str,
        trigger: str,
        bucket_id: str,
        recording_id: str,
    ) -> MessageEvent:
        user_id = str(creator.get("id") or "unknown")
        user_name = str(creator.get("name") or "Basecamp user")
        source = self.build_source(
            chat_id=target,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            message_id=message_id,
            scope_id=bucket_id,
        )
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=user_id,
            user_name=user_name,
            source=source,
            raw_message=raw,
            message_id=message_id,
            timestamp=timestamp,
            metadata={
                "basecamp_event_key": event_key,
                "basecamp_trigger": trigger,
                "basecamp_bucket_id": bucket_id,
                "basecamp_recording_id": recording_id,
            },
        )

    def _user_allowed(self, person_id: str) -> bool:
        return bool(self._allowed_users and str(person_id) in self._allowed_users)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        match = TARGET_RE.match(str(chat_id))
        if not match:
            return SendResult(success=False, error=f"Invalid Basecamp target: {chat_id}")
        kind, bucket_id, recording_id = match.groups()
        if kind != "ping" and bucket_id not in self._project_ids:
            return SendResult(success=False, error=f"Basecamp project not approved: {bucket_id}")
        try:
            if kind in {"ping", "chat"}:
                outbound_content = _ping_plain_text(content) if kind == "ping" else content
                payload = await self._cli_json(
                    "--account",
                    self._account_id,
                    "api",
                    "post",
                    f"/buckets/{bucket_id}/chats/{recording_id}/lines.json",
                    "--data",
                    json.dumps({"content": outbound_content}),
                    "--json",
                )
            else:
                payload = await self._cli_json(
                    "--account",
                    self._account_id,
                    "comments",
                    "create",
                    recording_id,
                    "-",
                    "--in",
                    bucket_id,
                    "--json",
                    stdin=content,
                )
            data = _data_from_envelope(payload)
            message_id = None
            if isinstance(data, dict):
                message_id = str(data.get("id") or data.get("comment", {}).get("id") or "") or None
            return SendResult(success=True, message_id=message_id, raw_response=payload)
        except Exception as exc:
            logger.error("[basecamp] Send failed target=%s: %s", chat_id, exc, exc_info=True)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        match = TARGET_RE.match(str(chat_id))
        if not match:
            return {"name": str(chat_id), "type": "group"}
        kind, bucket_id, recording_id = match.groups()
        return {
            "name": f"Basecamp {kind} {recording_id}",
            "type": "dm" if kind == "ping" else "group",
            "bucket_id": bucket_id,
            "recording_id": recording_id,
        }

    async def _cli_json(self, *args: str, stdin: Optional[str] = None) -> Any:
        command = [self._binary, "--profile", self._profile, *map(str, args)]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode() if stdin is not None else None),
                timeout=self._command_timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise BasecampCliError(f"Basecamp CLI timed out after {self._command_timeout:.0f}s")
        output = stdout.decode(errors="replace").strip()
        error = stderr.decode(errors="replace").strip()
        if process.returncode != 0:
            detail = output or error or f"exit {process.returncode}"
            try:
                parsed = json.loads(detail)
                detail = str(parsed.get("error") or detail) if isinstance(parsed, dict) else detail
            except json.JSONDecodeError:
                pass
            raise BasecampCliError(detail[:1000])
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise BasecampCliError(f"Basecamp CLI returned invalid JSON: {output[:300]}") from exc

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            loaded = json.loads(self._state_path.read_text())
            if isinstance(loaded, dict) and loaded.get("version") == 1:
                self._state.update(loaded)
        except Exception:
            logger.warning("[basecamp] Ignoring corrupt state at %s", self._state_path, exc_info=True)

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temp.write_text(json.dumps(self._state, indent=2, sort_keys=True) + "\n")
        os.chmod(temp, 0o600)
        temp.replace(self._state_path)


def _env_enablement() -> Optional[dict]:
    account_id = os.getenv("BASECAMP_ACCOUNT_ID", "").strip()
    person_id = os.getenv("BASECAMP_PERSON_ID", "").strip()
    project_ids = os.getenv("BASECAMP_PROJECT_IDS", "").strip()
    if not (account_id and person_id and project_ids):
        return None
    return {
        "account_id": account_id,
        "person_id": person_id,
        "project_ids": project_ids,
        "profile": os.getenv("BASECAMP_CLI_PROFILE", DEFAULT_PROFILE).strip() or DEFAULT_PROFILE,
        "allowed_users": os.getenv("BASECAMP_ALLOWED_USERS", "").strip(),
    }


def register(ctx) -> None:
    ctx.register_platform(
        name="basecamp",
        label="Basecamp",
        adapter_factory=lambda cfg: BasecampAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        env_enablement_fn=_env_enablement,
        allowed_users_env="BASECAMP_ALLOWED_USERS",
        allow_all_env="BASECAMP_ALLOW_ALL_USERS",
        max_message_length=10000,
        emoji="⛺",
        pii_safe=False,
        allow_update_command=False,
        platform_hint=(
            "You are responding to an explicit Basecamp Ping, verified @Hermes mention, "
            "assignment, or a new comment in a subscribed work thread. The adapter posts "
            "your final answer back to that exact Basecamp Ping, Campfire, or work item as "
            "Hermes (Agent). Be concise. Do not create or change other Basecamp work unless "
            "the request explicitly asks you to."
        ),
    )
