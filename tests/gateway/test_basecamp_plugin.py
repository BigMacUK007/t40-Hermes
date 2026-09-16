"""Focused tests for the Basecamp platform adapter."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from gateway.config import PlatformConfig
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_basecamp = load_plugin_adapter("basecamp")
BasecampAdapter = _basecamp.BasecampAdapter

PERSON_ID = "52943345"
BEN_ID = "47951398"
PROJECT_ID = "47430236"
ACCOUNT_ID = "5934104"
TRANSCRIPT_ID = "9929529992"
PING_TRANSCRIPT_ID = "10201309076"
CIRCLE_ID = "48499598"
MENTION_SGID = "hermes-mention-sgid"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _adapter(tmp_path, **extra):
    values = {
        "cli_path": "/usr/bin/true",
        "profile": "hermes",
        "account_id": ACCOUNT_ID,
        "person_id": PERSON_ID,
        "project_ids": [PROJECT_ID],
        "allowed_users": [BEN_ID],
        "state_path": str(tmp_path / "state.json"),
        "mark_read": False,
        "attachable_sgid": MENTION_SGID,
    }
    values.update(extra)
    return BasecampAdapter(PlatformConfig(enabled=True, extra=values))


def _creator(person_id=BEN_ID, name="Ben Macdonald"):
    return {"id": int(person_id), "name": name}


def _ping_reading():
    return {
        "id": 4927246296,
        "type": "Chat",
        "section": "pings",
        "app_url": f"https://app.basecamp.com/{ACCOUNT_ID}/circles/{CIRCLE_ID}",
        "bucket_name": "Ben Macdonald + Hermes (Agent)",
        "content_excerpt": "Hi Hermes",
        "created_at": "2026-08-13T19:14:43.269Z",
        "unread_at": "2026-08-13T19:14:43.268Z",
        "creator": _creator(),
        "participants": [_creator()],
        "readable_identifier": "Z2lkOi8vYmMzL1JlY29yZGluZy8xMDIwMTMwOTA3Ng",
        "readable_sgid": "ping-readable",
    }


def _mention_reading():
    return {
        "id": 4927122906,
        "type": "Chat",
        "section": "chats",
        "app_url": f"https://app.basecamp.com/{ACCOUNT_ID}/buckets/{PROJECT_ID}/chats/{TRANSCRIPT_ID}",
        "bucket_name": "T40 HQ",
        "content_excerpt": "please reply",
        "created_at": "2026-08-13T18:22:13.590Z",
        "unread_at": "2026-08-13T18:22:13.585Z",
        "creator": _creator(),
        "readable_sgid": "mention-readable",
    }


def _recording_mention_reading():
    return {
        "id": 4948728985,
        "type": "Mention",
        "section": "inbox",
        "app_url": (
            f"https://app.basecamp.com/{ACCOUNT_ID}/buckets/{PROJECT_ID}"
            "/messages/10230129645#__recording_10230143491"
        ),
        "bucket_name": "T40 HQ",
        "title": "@mentioned you in: Re: Hermes Daily Session Recap",
        "content_excerpt": "Hello testing if you respond Hermes",
        "created_at": "2026-08-23T23:12:22.095Z",
        "unread_at": "2026-08-23T23:12:22.093Z",
        "creator": _creator(),
        "readable_sgid": "recording-mention-readable",
    }


def _subscribed_comment_reading(subscribed=True):
    return {
        "id": 5001760938,
        "type": "Comment",
        "section": "inbox",
        "app_url": (
            f"https://app.basecamp.com/{ACCOUNT_ID}/buckets/{PROJECT_ID}"
            "/card_tables/cards/10309036114#__recording_10309096926"
        ),
        "bucket_name": "Compleye System",
        "title": "Re: Decide browser testing for the redesign (GitHub #93)",
        "content_excerpt": "can you implement this",
        "created_at": "2026-09-16T10:00:04.108Z",
        "unread_at": "2026-09-16T10:00:04.107Z",
        "creator": _creator(),
        "readable_sgid": "comment-readable",
        "subscribed": subscribed,
    }


def _assignment_reading():
    return {
        "id": 4927122919,
        "type": "Assignment",
        "section": "inbox",
        "app_url": f"https://app.basecamp.com/{ACCOUNT_ID}/buckets/{PROJECT_ID}/todos/10201071109",
        "bucket_name": "T40 HQ",
        "title": "Assigned you: Test task",
        "content_excerpt": "Test task",
        "created_at": "2026-08-13T18:22:13.975Z",
        "unread_at": "2026-08-13T18:22:13.975Z",
        "creator": _creator(),
        "readable_sgid": "assignment-readable",
    }


def _assignment(todo_id="10201071109", creator_id=BEN_ID, project_id=PROJECT_ID):
    return {
        "id": int(todo_id),
        "content": "Test task",
        "created_at": "2026-08-13T18:22:13.873Z",
        "updated_at": "2026-08-13T18:22:13.975Z",
        "creator": _creator(creator_id),
        "bucket": {"id": int(project_id), "name": "T40 HQ"},
        "assignees": [{"id": int(PERSON_ID), "name": "Hermes (Agent)"}],
        "children": [],
    }


def test_plugin_registration():
    ctx = MagicMock()
    _basecamp.register(ctx)
    call = ctx.register_platform.call_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["name"] == "basecamp"
    assert kwargs["allowed_users_env"] == "BASECAMP_ALLOWED_USERS"
    assert kwargs["allow_all_env"] == "BASECAMP_ALLOW_ALL_USERS"


def test_readable_identifier_decodes_ping_transcript():
    assert (
        _basecamp._readable_recording_id(
            "Z2lkOi8vYmMzL1JlY29yZGluZy8xMDIwMTMwOTA3Ng"
        )
        == PING_TRANSCRIPT_ID
    )


def test_ping_reading_builds_dm_target(tmp_path):
    adapter = _adapter(tmp_path)
    event = _run(adapter._event_from_reading(_ping_reading()))
    assert event is not None
    assert event.source.chat_id == f"ping:{CIRCLE_ID}:{PING_TRANSCRIPT_ID}"
    assert event.source.chat_type == "dm"
    assert event.user_id == BEN_ID
    assert event.metadata["basecamp_trigger"] == "ping"


def test_assignment_reading_builds_recording_target(tmp_path):
    adapter = _adapter(tmp_path)
    event = _run(adapter._event_from_reading(_assignment_reading()))
    assert event is not None
    assert event.source.chat_id == f"recording:{PROJECT_ID}:10201071109"
    assert event.text == "Basecamp assignment: Test task"
    assert event.metadata["basecamp_trigger"] == "assignment"


def test_unapproved_project_is_dropped(tmp_path):
    adapter = _adapter(tmp_path)
    reading = _assignment_reading()
    reading["app_url"] = (
        f"https://app.basecamp.com/{ACCOUNT_ID}/buckets/999/todos/10201071109"
    )
    assert _run(adapter._event_from_reading(reading)) is None


def test_self_authored_reading_is_dropped(tmp_path):
    adapter = _adapter(tmp_path)
    reading = _ping_reading()
    reading["creator"] = _creator(PERSON_ID, "Hermes (Agent)")
    assert _run(adapter._event_from_reading(reading)) is None


def test_unknown_user_is_dropped(tmp_path):
    adapter = _adapter(tmp_path)
    reading = _ping_reading()
    reading["creator"] = _creator("123", "Unknown")
    assert _run(adapter._event_from_reading(reading)) is None


def test_chat_reading_requires_structured_mention(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._cli_json = AsyncMock(
        return_value=[
            {
                "id": 1,
                "content": "ordinary subscribed chat line",
                "creator": _creator(),
                "created_at": "2026-08-13T18:22:13.411Z",
            }
        ]
    )
    assert _run(adapter._event_from_reading(_mention_reading())) is None


def test_structured_chat_mention_builds_chat_target(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._cli_json = AsyncMock(
        return_value=[
            {
                "id": 10201071081,
                "content": (
                    f'<bc-attachment sgid="{MENTION_SGID}" '
                    'content-type="application/vnd.basecamp.mention"></bc-attachment> '
                    "please reply"
                ),
                "creator": _creator(),
                "created_at": "2026-08-13T18:22:13.411Z",
            }
        ]
    )
    event = _run(adapter._event_from_reading(_mention_reading()))
    assert event is not None
    assert event.source.chat_id == f"chat:{PROJECT_ID}:{TRANSCRIPT_ID}"
    assert event.metadata["basecamp_trigger"] == "mention"


def test_verified_recording_mention_builds_parent_recording_target(tmp_path):
    adapter = _adapter(tmp_path)
    event = _run(adapter._event_from_reading(_recording_mention_reading()))
    assert event is not None
    assert event.source.chat_id == f"recording:{PROJECT_ID}:10230129645"
    assert event.text == "Hello testing if you respond Hermes"
    assert event.metadata["basecamp_trigger"] == "mention"


def test_subscribed_comment_builds_parent_recording_target_when_enabled(tmp_path):
    adapter = _adapter(tmp_path, follow_subscribed_comments=True)
    event = _run(adapter._event_from_reading(_subscribed_comment_reading()))
    assert event is not None
    assert event.source.chat_id == f"recording:{PROJECT_ID}:10309036114"
    assert event.text == "can you implement this"
    assert event.metadata["basecamp_trigger"] == "subscribed_comment"


def test_subscribed_comment_is_dropped_when_following_is_disabled(tmp_path):
    adapter = _adapter(tmp_path, follow_subscribed_comments=False)
    assert _run(adapter._event_from_reading(_subscribed_comment_reading())) is None


def test_unsubscribed_comment_is_dropped_when_following_is_enabled(tmp_path):
    adapter = _adapter(tmp_path, follow_subscribed_comments=True)
    assert _run(adapter._event_from_reading(_subscribed_comment_reading(False))) is None


def test_assignment_safety_net_requires_hermes_assignee(tmp_path):
    adapter = _adapter(tmp_path)
    assignment = _assignment()
    assignment["assignees"] = [{"id": 123, "name": "Other"}]
    assert adapter._event_from_assignment(assignment) is None


def test_first_poll_bootstraps_without_dispatch(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._cli_json = AsyncMock(
        side_effect=[
            {"unreads": [_ping_reading()]},
            {"priorities": [], "non_priorities": [_assignment()]},
        ]
    )
    adapter.handle_message = AsyncMock()
    dispatched = _run(adapter.poll_once(dispatch=False))
    assert dispatched == 0
    adapter.handle_message.assert_not_called()
    state = json.loads((tmp_path / "state.json").read_text())
    assert "4927246296:2026-08-13T19:14:43.268Z" in state["seen_readings"]
    assert "10201071109" in state["assignment_ids"]


def test_second_poll_dispatches_only_new_ping(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._state.update(
        {
            "bootstrapped": True,
            "seen_readings": ["1"],
            "assignment_ids": ["10201071109"],
        }
    )
    adapter._cli_json = AsyncMock(
        side_effect=[
            {"unreads": [_ping_reading()]},
            {"priorities": [], "non_priorities": [_assignment()]},
        ]
    )
    adapter.handle_message = AsyncMock()
    dispatched = _run(adapter.poll_once(dispatch=True))
    assert dispatched == 1
    call = adapter.handle_message.await_args
    assert call is not None
    event = call.args[0]
    assert event.source.chat_id == f"ping:{CIRCLE_ID}:{PING_TRANSCRIPT_ID}"


def test_same_ping_reading_id_with_new_unread_time_dispatches(tmp_path):
    adapter = _adapter(tmp_path)
    old_key = "4927246296:2026-08-13T19:14:43.268Z"
    updated = _ping_reading()
    updated["content_excerpt"] = "Follow-up Ping"
    updated["unread_at"] = "2026-08-13T19:47:21.935Z"
    adapter._state.update(
        {
            "bootstrapped": True,
            "seen_readings": [old_key],
            "assignment_ids": ["10201071109"],
        }
    )
    adapter._cli_json = AsyncMock(
        side_effect=[
            {"unreads": [updated]},
            {"priorities": [], "non_priorities": [_assignment()]},
        ]
    )
    adapter.handle_message = AsyncMock()

    dispatched = _run(adapter.poll_once(dispatch=True))

    assert dispatched == 1
    call = adapter.handle_message.await_args
    assert call is not None
    event = call.args[0]
    assert event.text == "Follow-up Ping"
    assert event.metadata["basecamp_event_key"] == (
        "reading:4927246296:2026-08-13T19:47:21.935Z"
    )


def test_poll_marks_processed_reading_ids_with_cli_command(tmp_path):
    adapter = _adapter(tmp_path, mark_read=True)
    adapter._state.update(
        {
            "bootstrapped": True,
            "seen_readings": [],
            "assignment_ids": ["10201071109"],
        }
    )
    adapter._cli_json = AsyncMock(
        side_effect=[
            {"unreads": [_ping_reading()]},
            {"priorities": [], "non_priorities": [_assignment()]},
            {"ok": True, "data": {"marked_read": 1}},
        ]
    )
    adapter.handle_message = AsyncMock()

    dispatched = _run(adapter.poll_once(dispatch=True))

    assert dispatched == 1
    mark_call = adapter._cli_json.await_args_list[2]
    assert mark_call.args == (
        "--account",
        ACCOUNT_ID,
        "notifications",
        "read",
        "4927246296",
        "--json",
    )


def test_ping_plain_text_removes_markdown_and_structured_breaks():
    content = (
        "At the moment I can access:\n\n"
        "**Account:** T40 Digital  \n"
        "**Project:** T40 HQ\n\n"
        "- Message Board\n"
        "- [Docs](https://example.com)"
    )
    assert _basecamp._ping_plain_text(content) == (
        "At the moment I can access: Account: T40 Digital Project: T40 HQ "
        "Message Board Docs (https://example.com)"
    )


def test_send_ping_uses_circle_bucket_and_transcript(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._cli_json = AsyncMock(return_value={"ok": True, "data": {"id": 55}})
    result = _run(
        adapter.send(
            f"ping:{CIRCLE_ID}:{PING_TRANSCRIPT_ID}",
            "**Hello**\n\n- from Hermes",
        )
    )
    assert result.success is True
    call = adapter._cli_json.await_args
    assert call is not None
    args = call.args
    assert f"/buckets/{CIRCLE_ID}/chats/{PING_TRANSCRIPT_ID}/lines.json" in args
    assert json.dumps({"content": "Hello from Hermes"}) in args


def test_send_recording_comments_on_item(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._cli_json = AsyncMock(return_value={"ok": True, "data": {"id": 66}})
    result = _run(
        adapter.send(f"recording:{PROJECT_ID}:10201071109", "Done")
    )
    assert result.success is True
    call = adapter._cli_json.await_args
    assert call is not None
    args = call.args
    assert "comments" in args
    assert "10201071109" in args
    assert call.kwargs["stdin"] == "Done"


def test_send_rejects_unapproved_project(tmp_path):
    adapter = _adapter(tmp_path)
    result = _run(adapter.send("recording:999:123", "No"))
    assert result.success is False
    assert "not approved" in result.error


def test_state_round_trip_is_mode_600(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._state["seen_readings"] = ["7"]
    adapter._save_state()
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
    other = _adapter(tmp_path)
    other._load_state()
    assert other._state["seen_readings"] == ["7"]
