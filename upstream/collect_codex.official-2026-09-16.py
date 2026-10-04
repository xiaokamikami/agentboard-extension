cat > "$CODEX_COLLECT_FILE" <<'CODEXCOLLECTEOF'
#!/usr/bin/env python3
"""
AgentBoard Codex session data collector.

PRIVACY: This script ONLY extracts aggregate numeric stats from Codex session
files. It NEVER reads, stores, or transmits conversation content, code,
prompts, or responses.
"""

import glob
import hashlib
import json
import os
import platform as platform_module
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

MAX_MINS_PER_DAY = 960
MAX_MINS_PER_SESSION = 480
HUMAN_TOKENS_PER_MIN = 300
SYNC_INTERVAL_SECS = 300
CODEX_IDLE_GAP_SECS = 10 * 60
SESSION_TAIL_SECS = 2 * 60
AGENTBOARD_SCRIPT_RELEASE = "2026-09-16"
__version__ = AGENTBOARD_SCRIPT_RELEASE
CODEX_MEASUREMENT_VERSION = "codex-v2-shadow.7"
CODEX_MEASUREMENT_REVISION = 7
CODEX_SYNC_STATE_VERSION = f"{AGENTBOARD_SCRIPT_RELEASE}:{CODEX_MEASUREMENT_VERSION}"
SHADOW_SETTLED_STATUSES = (
    "accepted",
    "already_current",
    "rejected_older_revision",
)
CODEX_REPLAY_GAP_SECS = 10
CODEX_REPLAY_MIN_EVENTS = 100
CODEX_MESSAGE_MIRROR_WINDOW_SECS = 1
CODEX_PROVIDER_TOTAL_POLICY_START_DATE = "2026-04-21"
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$")
COMMON_CA_BUNDLE_PATHS = (
    "/etc/ssl/cert.pem",
    "/private/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
)


def build_ssl_context():
    candidates = []
    env_bundle = os.environ.get("SSL_CERT_FILE")
    if env_bundle:
        candidates.append(env_bundle)

    try:
        import certifi  # type: ignore

        candidates.append(certifi.where())
    except Exception:
        pass

    try:
        defaults = ssl.get_default_verify_paths()
        candidates.extend([defaults.cafile, defaults.openssl_cafile])
    except Exception:
        pass

    candidates.extend(COMMON_CA_BUNDLE_PATHS)
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen or not os.path.exists(candidate):
            continue
        seen.add(candidate)
        try:
            return ssl.create_default_context(cafile=candidate)
        except Exception:
            continue

    return ssl.create_default_context()


def sanitize_host_id(value):
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "").strip()).strip("._")
    return sanitized[:80] or "unknown"


def detect_device_name():
    env_value = os.environ.get("AGENTBOARD_DEVICE_NAME", "").strip()
    if env_value:
        return env_value
    try:
        hostname = socket.gethostname().strip()
        if hostname:
            return hostname
    except Exception:
        pass
    return ""


def get_host_id():
    return sanitize_host_id(os.environ.get("AGENTBOARD_HOST_ID", "") or detect_device_name())


def normalize_device_platform(value):
    normalized = (value or "").strip().lower()
    if not normalized:
        return ""
    if normalized.startswith("darwin") or normalized in ("mac", "macos", "mac os", "mac os x"):
        return "macos"
    if normalized.startswith("win") or "windows" in normalized:
        return "win32"
    if normalized.startswith("linux") or "linux" in normalized:
        return "linux"
    return normalized


def detect_platform_name():
    env_value = os.environ.get("AGENTBOARD_PLATFORM", "").strip()
    if env_value:
        return normalize_device_platform(env_value)
    try:
        return normalize_device_platform(platform_module.system())
    except Exception:
        return ""


SSL_CONTEXT = build_ssl_context()
AGENTBOARD_DIR = os.path.expanduser("~/.agentboard")
HOST_ID = get_host_id()
LOG_DIR = os.path.join(AGENTBOARD_DIR, "logs")
SYNC_LOG_PATH = os.path.join(LOG_DIR, "codex-sync.log")
LAST_SUCCESS_PATH = os.path.join(LOG_DIR, "last-success.txt")
SYNC_LOCK_PATH = os.path.join(AGENTBOARD_DIR, f"codex-sync.{HOST_ID}.lock")
MAX_LOG_BYTES = 10 * 1024 * 1024


def rotate_log(path):
    try:
        if not os.path.exists(path) or os.path.getsize(path) < MAX_LOG_BYTES:
            return
        backup_one = path + ".1"
        backup_two = path + ".2"
        if os.path.exists(backup_two):
            os.remove(backup_two)
        if os.path.exists(backup_one):
            os.replace(backup_one, backup_two)
        os.replace(path, backup_one)
    except Exception:
        pass


def log_sync(message):
    line = f"[agentboard-codex] {message}"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rotate_log(SYNC_LOG_PATH)
        with open(SYNC_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if sys.stderr.isatty():
        print(line, file=sys.stderr)


def log_sync_error(context, error):
    detail = str(error)
    if isinstance(error, urllib.error.HTTPError):
        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        detail = f"HTTP {error.code}: {error.reason}"
        if body:
            detail += f" body={body}"
    log_sync(f"{context}: {detail}")


def mark_last_success():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LAST_SUCCESS_PATH, "w", encoding="utf-8") as f:
            f.write(datetime.now().astimezone().isoformat() + "\n")
    except Exception:
        pass


def emit_json(payload):
    try:
        print(json.dumps(payload))
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass


def clamp_minutes(active_seconds, max_minutes):
    if active_seconds <= 0:
        return 0
    return min(max_minutes, int(round(active_seconds / 60.0)))


def build_engaged_windows(events, gap_cap_secs, tail_secs):
    if not events:
        return []

    windows = []
    sorted_events = sorted(events, key=lambda e: e[1])
    for index, (_, ts) in enumerate(sorted_events):
        start = ts.timestamp()
        if index + 1 < len(sorted_events):
            next_start = sorted_events[index + 1][1].timestamp()
            end = min(start + gap_cap_secs, next_start)
        else:
            end = start + tail_secs
        if end > start:
            windows.append((start, end))

    if not windows:
        return []

    merged = [list(windows[0])]
    for start, end in windows[1:]:
        last = merged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])

    return merged


def estimate_engaged_seconds(events, gap_cap_secs, tail_secs):
    windows = build_engaged_windows(events, gap_cap_secs, tail_secs)
    return int(sum(end - start for start, end in windows))


def windows_to_payload(windows):
    return [
        {
            "start_at": datetime.fromtimestamp(start).astimezone().isoformat(),
            "end_at": datetime.fromtimestamp(end).astimezone().isoformat(),
        }
        for start, end in windows
    ]


def build_tool_breakdown(tool_counts, top_n=4):
    total_calls = sum(tool_counts.values())
    if total_calls <= 0:
        return []

    ordered = sorted(
        tool_counts.items(),
        key=lambda item: (-item[1], item[0].lower()),
    )[:top_n]
    return [
        {
            "tool": tool,
            "count": count,
            "percentage": int(round((count / total_calls) * 100)),
        }
        for tool, count in ordered
    ]


def new_day():
    return {
        "events": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "effective_input_tokens": 0,
        "effective_output_tokens": 0,
        "effective_cache_read_tokens": 0,
        "unattributed_input_tokens": 0,
        "unattributed_output_tokens": 0,
        "unattributed_cache_read_tokens": 0,
        "accounting_methods": set(),
        "token_event_count": 0,
        "counter_epoch_count": 0,
        "has_unresolved_lineage": False,
        "user_msgs": 0,
        "assistant_msgs": 0,
        "lines_added": 0,
        "lines_removed": 0,
        "projects": set(),
        "tool_calls": 0,
        "tool_counts": defaultdict(int),
        "files_touched": set(),
        "tombstone": False,
    }


def normalize_ts(ts_str):
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


def safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def load_session_entries(session_file):
    entries = []
    with open(session_file, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = normalize_ts(entry.get("timestamp"))
            if not ts:
                continue

            entries.append((line_number, entry, ts))

    return entries


def usage_tuple(value):
    if not isinstance(value, dict):
        value = {}
    return (
        safe_int(value.get("input_tokens", 0)),
        safe_int(value.get("output_tokens", 0)),
        safe_int(value.get("cached_input_tokens", 0)),
    )


def token_usage_from_entry(entry):
    if entry.get("type") != "event_msg":
        return None

    payload = entry.get("payload", {})
    if payload.get("type") != "token_count":
        return None

    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    total_usage = usage_tuple(info.get("total_token_usage"))
    raw_last_usage = info.get("last_token_usage")
    last_usage = usage_tuple(raw_last_usage) if isinstance(raw_last_usage, dict) else None

    return {"total": total_usage, "last": last_usage}


def rollout_ordinal(entry):
    value = entry.get("ordinal")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def metadata_has_subagent_signal(value):
    if isinstance(value, str):
        text = value.lower()
    elif isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, sort_keys=True).lower()
        except (TypeError, ValueError):
            return False
    else:
        return False
    return "subagent" in text or "sub_agent" in text


def session_lineage_plan(entries, skip_before_line):
    session_meta = None
    for _, entry, _ in entries:
        if entry.get("type") == "session_meta":
            payload = entry.get("payload", {})
            session_meta = payload if isinstance(payload, dict) else {}
            break

    if session_meta is None:
        return {"mode": "standard", "boundary": None}

    boundary_value = session_meta.get("subagent_history_start_ordinal")
    boundary = (
        boundary_value
        if isinstance(boundary_value, int)
        and not isinstance(boundary_value, bool)
        and boundary_value >= 1
        else None
    )
    if boundary is not None:
        token_ordinals = [
            rollout_ordinal(entry)
            for _, entry, _ in entries
            if token_usage_from_entry(entry) is not None
        ]
        ordinals_are_complete = all(value is not None for value in token_ordinals)
        ordinals_are_ordered = ordinals_are_complete and all(
            token_ordinals[index] < token_ordinals[index + 1]
            for index in range(len(token_ordinals) - 1)
        )
        if ordinals_are_complete and ordinals_are_ordered:
            return {"mode": "ordinal_boundary", "boundary": boundary}
        return {"mode": "unresolved_lineage", "boundary": None}

    # A successful legacy replay-gap split already isolated the local suffix.
    if skip_before_line not in (1, sys.maxsize):
        return {"mode": "legacy_gap_boundary", "boundary": None}

    history_base = session_meta.get("history_base")
    forked_from_id = session_meta.get("forked_from_id")
    parent_thread_id = session_meta.get("parent_thread_id")
    history_mode = str(session_meta.get("history_mode") or "legacy").lower()
    has_subagent_signal = any(
        metadata_has_subagent_signal(session_meta.get(key))
        for key in ("thread_source", "source", "originator")
    )

    # Referenced paginated forks keep inherited records in the parent rollout, so
    # this physical file contains only local records and is safe to measure.
    if history_mode == "paginated" and isinstance(history_base, dict):
        return {"mode": "referenced_history", "boundary": None}

    # Legacy copied forks do not carry ordinals or a durable local-history
    # boundary. Their token counters cannot be separated without guessing.
    if forked_from_id or (
        history_mode != "paginated" and (parent_thread_id or has_subagent_signal)
    ):
        return {
            "mode": "unresolved_lineage",
            "boundary": None,
            "method": "legacy_fork_without_boundary",
        }

    return {"mode": "standard", "boundary": None}


def derive_v2_usage(usage, previous_total):
    total = usage["total"]
    last = usage.get("last")
    zero = (0, 0, 0)
    last_is_valid = last is not None and all(
        0 <= last[index] <= total[index] for index in range(3)
    )

    if previous_total is None:
        effective = last if last_is_valid else zero
        unattributed = tuple(
            max(0, total[index] - effective[index]) for index in range(3)
        )
        method = "last_usage" if last_is_valid else "unattributed_baseline"
        return effective, unattributed, total, method, True

    if any(total[index] < previous_total[index] for index in range(3)):
        effective = last if last_is_valid else zero
        unattributed = tuple(
            max(0, total[index] - effective[index]) for index in range(3)
        )
        method = "last_usage_after_reset" if last_is_valid else "unattributed_reset"
        return effective, unattributed, total, method, True

    delta = tuple(
        max(0, total[index] - previous_total[index]) for index in range(3)
    )
    method = "duplicate_total" if delta == zero else "cumulative_delta"
    return delta, zero, total, method, False


def derive_unattributed_usage(usage, previous_total, method):
    total = usage["total"]
    zero = (0, 0, 0)
    if previous_total is None or any(
        total[index] < previous_total[index] for index in range(3)
    ):
        return zero, total, total, method, True

    delta = tuple(
        max(0, total[index] - previous_total[index]) for index in range(3)
    )
    return zero, delta, total, method, False


def measurement_metadata(data):
    unresolved_lineage = data.get("has_unresolved_lineage", False)
    effective_total = data["effective_input_tokens"] + data["effective_output_tokens"]
    unattributed_total = (
        data["unattributed_input_tokens"] + data["unattributed_output_tokens"]
    )
    effective_is_unknown = unresolved_lineage or (
        unattributed_total > 0 and effective_total == 0
    )
    if effective_is_unknown:
        status = "unresolved"
    elif unattributed_total > 0 and effective_total > 0:
        status = "partial"
    elif unattributed_total > 0:
        status = "unresolved"
    elif data["token_event_count"] > 0:
        status = "verified"
    else:
        status = "empty"

    methods = sorted(data["accounting_methods"])
    digest_payload = {
        "effective_input_tokens": (
            None if effective_is_unknown else data["effective_input_tokens"]
        ),
        "effective_output_tokens": (
            None if effective_is_unknown else data["effective_output_tokens"]
        ),
        "effective_cache_read_tokens": (
            None if effective_is_unknown else data["effective_cache_read_tokens"]
        ),
        "effective_ai_time_mins": (
            None
            if effective_is_unknown
            else (
                max(
                    1,
                    int(data["effective_output_tokens"] / HUMAN_TOKENS_PER_MIN),
                )
                if data["effective_output_tokens"] > 0
                else 0
            )
        ),
        "unattributed_input_tokens": data["unattributed_input_tokens"],
        "unattributed_output_tokens": data["unattributed_output_tokens"],
        "unattributed_cache_read_tokens": data["unattributed_cache_read_tokens"],
        "token_event_count": data["token_event_count"],
        "counter_epoch_count": data["counter_epoch_count"],
        "accounting_methods": methods,
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return {
        "measurement_version": CODEX_MEASUREMENT_VERSION,
        "measurement_revision": CODEX_MEASUREMENT_REVISION,
        "measurement_status": status,
        "measurement_digest": digest,
        "accounting_method": "+".join(methods) or "none",
        "effective_provider_total_tokens": (
            None if effective_is_unknown else effective_total
        ),
        "effective_input_tokens": (
            None if effective_is_unknown else data["effective_input_tokens"]
        ),
        "effective_output_tokens": (
            None if effective_is_unknown else data["effective_output_tokens"]
        ),
        "effective_cache_read_tokens": (
            None if effective_is_unknown else data["effective_cache_read_tokens"]
        ),
        "effective_ai_time_mins": digest_payload["effective_ai_time_mins"],
        "unattributed_tokens": unattributed_total,
        "unattributed_input_tokens": data["unattributed_input_tokens"],
        "unattributed_output_tokens": data["unattributed_output_tokens"],
        "unattributed_cache_read_tokens": data["unattributed_cache_read_tokens"],
        "token_event_count": data["token_event_count"],
        "counter_epoch_count": data["counter_epoch_count"],
    }


def detect_replay_prefix(entries):
    leading_meta_count = 0
    for _, entry, _ in entries:
        if entry.get("type") == "session_meta":
            leading_meta_count += 1
            continue
        break

    if leading_meta_count < 2:
        return 1, (0, 0, 0)

    previous_ts = None
    parsed_count = 0
    baseline_input = 0
    baseline_output = 0
    baseline_cached = 0

    for line_number, entry, ts in entries:
        if (
            previous_ts is not None
            and parsed_count >= CODEX_REPLAY_MIN_EVENTS
            and (ts - previous_ts).total_seconds() >= CODEX_REPLAY_GAP_SECS
        ):
            return line_number, (baseline_input, baseline_output, baseline_cached)

        usage = token_usage_from_entry(entry)
        if usage:
            total_input, total_output, total_cached = usage["total"]
            baseline_input = max(baseline_input, total_input)
            baseline_output = max(baseline_output, total_output)
            baseline_cached = max(baseline_cached, total_cached)

        previous_ts = ts
        parsed_count += 1

    return sys.maxsize, (baseline_input, baseline_output, baseline_cached)


def primary_session_metadata(entries):
    """Return metadata for the physical rollout, before any inherited meta."""
    for _, entry, _ in entries:
        if entry.get("type") != "session_meta":
            continue
        payload = entry.get("payload", {})
        return payload if isinstance(payload, dict) else {}
    return {}


def message_lineage_scope(entries, skip_before_line):
    """Choose the strongest available boundary for locally-created messages.

    Official paginated-history metadata takes precedence over the legacy replay
    gap heuristic. Real paginated subagent rollouts can start with two session
    metadata records and have no ten-second gap, while still providing an exact
    local-history ordinal boundary.
    """
    session_meta = primary_session_metadata(entries)

    boundary_value = session_meta.get("subagent_history_start_ordinal")
    if (
        isinstance(boundary_value, int)
        and not isinstance(boundary_value, bool)
        and boundary_value >= 1
    ):
        return {"mode": "ordinal_boundary", "boundary": boundary_value}

    history_mode = str(session_meta.get("history_mode") or "legacy").lower()
    history_base = session_meta.get("history_base")
    if history_mode == "paginated" and isinstance(history_base, dict):
        referenced_boundary = history_base.get("end_ordinal_exclusive")
        if not (
            isinstance(referenced_boundary, int)
            and not isinstance(referenced_boundary, bool)
            and referenced_boundary >= 0
        ):
            referenced_boundary = None
        return {"mode": "referenced_history", "boundary": referenced_boundary}

    leading_meta_count = 0
    for _, entry, _ in entries:
        if entry.get("type") != "session_meta":
            break
        leading_meta_count += 1

    if leading_meta_count >= 2:
        if skip_before_line == sys.maxsize:
            return {"mode": "unresolved_replay", "boundary": None}
        return {"mode": "legacy_gap_boundary", "boundary": skip_before_line}

    return {"mode": "standard", "boundary": 1}


def is_item_completed_message(entry):
    if entry.get("type") != "event_msg":
        return False
    payload = entry.get("payload", {})
    if payload.get("type") != "item_completed":
        return False
    item = payload.get("item", {})
    return isinstance(item, dict) and item.get("type") in (
        "UserMessage",
        "AgentMessage",
    )


def message_entry_is_local(line_number, entry, scope, session_id, is_canonical):
    payload = entry.get("payload", {})
    thread_id = payload.get("thread_id") if is_canonical else None
    if isinstance(thread_id, str) and thread_id and thread_id != session_id:
        return False

    mode = scope["mode"]
    ordinal = rollout_ordinal(entry)
    boundary = scope.get("boundary")

    if mode == "ordinal_boundary":
        if ordinal is not None:
            return ordinal >= boundary
        # Direct legacy events have no ownership field and cannot safely cross
        # an ordinal boundary. A canonical item can be rescued by exact thread
        # ownership when an older writer omitted its ordinal.
        return is_canonical and thread_id == session_id

    if mode == "referenced_history":
        # history_base is a reference, not copied history: the physical file is
        # the local suffix. Validate ordinals when present, but retain support
        # for older suffix writers that omitted them.
        if boundary is not None and ordinal is not None:
            return ordinal >= boundary
        return True

    if mode == "legacy_gap_boundary":
        return line_number >= boundary

    if mode == "unresolved_replay":
        # Even without a replay boundary, a completed item explicitly owned by
        # this thread is local. Anonymous legacy direct events remain excluded.
        return is_canonical and thread_id == session_id

    return True


def normalize_message_phase(value):
    return value if isinstance(value, str) else ""


def extract_message_candidates(entries, session_id, skip_before_line):
    """Extract visible messages without reading or hashing their content."""
    scope = message_lineage_scope(entries, skip_before_line)
    canonical = []
    legacy = []
    active_turn_id = ""

    for line_number, entry, ts in entries:
        payload = entry.get("payload", {})
        entry_type = entry.get("type")
        payload_type = payload.get("type")

        if entry_type == "event_msg" and payload_type == "task_started":
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                active_turn_id = turn_id
        elif entry_type == "turn_context":
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                active_turn_id = turn_id

        if is_item_completed_message(entry):
            if not message_entry_is_local(
                line_number, entry, scope, session_id, is_canonical=True
            ):
                continue
            item = payload["item"]
            role = "user" if item.get("type") == "UserMessage" else "assistant"
            turn_id = payload.get("turn_id") or active_turn_id
            if role == "user":
                stable_id = item.get("client_id") or item.get("id")
            else:
                stable_id = item.get("id")
            if isinstance(stable_id, str) and stable_id:
                key = ("canonical", role, stable_id)
            else:
                ordinal = rollout_ordinal(entry)
                key = (
                    "canonical_fallback",
                    session_id,
                    turn_id,
                    role,
                    ordinal if ordinal is not None else line_number,
                )
            canonical.append(
                {
                    "key": key,
                    "source": "canonical",
                    "role": role,
                    "turn_id": turn_id if isinstance(turn_id, str) else "",
                    "phase": normalize_message_phase(item.get("phase")),
                    "timestamp": ts,
                    "line_number": line_number,
                }
            )
        elif entry_type == "event_msg" and payload_type in (
            "user_message",
            "agent_message",
        ):
            if not message_entry_is_local(
                line_number, entry, scope, session_id, is_canonical=False
            ):
                continue
            role = "user" if payload_type == "user_message" else "assistant"
            stable_id = payload.get("id")
            ordinal = rollout_ordinal(entry)
            if isinstance(stable_id, str) and stable_id:
                key = ("legacy", role, stable_id)
            elif ordinal is not None:
                key = ("legacy_ordinal", session_id, role, ordinal)
            else:
                # Legacy direct records have no durable id. Their turn, phase,
                # role, and timestamp form the strongest privacy-safe identity.
                key = (
                    "legacy_fallback",
                    session_id,
                    active_turn_id,
                    role,
                    normalize_message_phase(payload.get("phase")),
                    ts.isoformat(),
                )
            legacy.append(
                {
                    "key": key,
                    "source": "legacy",
                    "role": role,
                    "turn_id": active_turn_id,
                    "phase": normalize_message_phase(payload.get("phase")),
                    "timestamp": ts,
                    "line_number": line_number,
                }
            )

        if entry_type == "event_msg" and payload_type == "task_complete":
            active_turn_id = ""

    def unique_candidates(candidates):
        unique = {}
        for candidate in candidates:
            unique.setdefault(candidate["key"], candidate)
        return list(unique.values())

    canonical = unique_candidates(canonical)
    legacy = unique_candidates(legacy)
    matched_legacy = set()

    # Some writers briefly dual-write direct and item_completed messages. Match
    # mirrors one-to-one rather than dropping every legacy record in that turn,
    # so a distinct legacy commentary next to a canonical final is preserved.
    for current in sorted(
        canonical,
        key=lambda candidate: (candidate["timestamp"], candidate["line_number"]),
    ):
        best_index = None
        best_distance = None
        for index, fallback in enumerate(legacy):
            if index in matched_legacy or fallback["role"] != current["role"]:
                continue
            if (
                current["turn_id"]
                and fallback["turn_id"]
                and current["turn_id"] != fallback["turn_id"]
            ):
                continue
            if (
                current["phase"]
                and fallback["phase"]
                and current["phase"] != fallback["phase"]
            ):
                continue
            distance = abs(
                (current["timestamp"] - fallback["timestamp"]).total_seconds()
            )
            if distance > CODEX_MESSAGE_MIRROR_WINDOW_SECS:
                continue
            if best_distance is None or distance < best_distance:
                best_index = index
                best_distance = distance
        if best_index is not None:
            matched_legacy.add(best_index)
            mirror = legacy[best_index]
            current["legacy_activity"] = {
                "role": mirror["role"],
                "timestamp": mirror["timestamp"],
            }

    combined = canonical + [
        candidate
        for index, candidate in enumerate(legacy)
        if index not in matched_legacy
    ]
    return sorted(
        combined,
        key=lambda candidate: (candidate["timestamp"], candidate["line_number"]),
    )


def iter_session_roots(root_dir):
    normalized_root = os.path.normpath(os.path.abspath(os.path.expanduser(root_dir)))
    roots = [normalized_root]
    archived_root = os.path.join(os.path.dirname(normalized_root), "archived_sessions")
    if (
        os.path.basename(normalized_root) == "sessions"
        and os.path.isdir(archived_root)
    ):
        roots.append(archived_root)

    seen = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        yield root


def iter_session_files(root_dir):
    seen = set()
    for root in iter_session_roots(root_dir):
        pattern = os.path.join(root, "**", "*.jsonl")
        for path in glob.glob(pattern, recursive=True):
            if os.path.isfile(path) and path not in seen:
                seen.add(path)
                yield path


def count_patch_lines(arguments):
    lines_added = 0
    lines_removed = 0
    files_touched = set()

    for line in arguments.splitlines():
        file_match = PATCH_FILE_RE.match(line)
        if file_match:
            files_touched.add(file_match.group(1).strip())
            continue

        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            lines_added += 1
        elif line.startswith("-"):
            lines_removed += 1

    return lines_added, lines_removed, files_touched


def _is_js_identifier_start(char):
    return bool(char) and (char.isalpha() or char in "_$")


def _is_js_identifier_part(char):
    return bool(char) and (char.isalnum() or char in "_$")


def _read_js_string_literal(source, start):
    quote = source[start]
    index = start + 1
    value = []
    dynamic_template = False
    simple_escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
    }

    while index < len(source):
        char = source[index]
        if char == quote:
            decoded = None if dynamic_template else "".join(value)
            return decoded, index + 1

        if quote == "`" and char == "$" and index + 1 < len(source):
            if source[index + 1] == "{":
                # Resolving template expressions would require executing or
                # fully interpreting JavaScript. Keep the collector local and
                # deterministic by treating dynamic templates as unsupported.
                dynamic_template = True
                index += 2
                continue

        if char != "\\":
            if char in "\r\n" and quote != "`":
                return None, index + 1
            if not dynamic_template:
                value.append(char)
            index += 1
            continue

        index += 1
        if index >= len(source):
            return None, index

        escaped = source[index]
        if escaped in "\r\n":
            if (
                escaped == "\r"
                and index + 1 < len(source)
                and source[index + 1] == "\n"
            ):
                index += 1
            index += 1
            continue

        if not dynamic_template:
            if escaped in simple_escapes:
                value.append(simple_escapes[escaped])
            elif escaped == "x" and index + 2 < len(source):
                digits = source[index + 1 : index + 3]
                try:
                    value.append(chr(int(digits, 16)))
                    index += 2
                except ValueError:
                    value.append(escaped)
            elif escaped == "u":
                if index + 1 < len(source) and source[index + 1] == "{":
                    close = source.find("}", index + 2)
                    digits = source[index + 2 : close] if close != -1 else ""
                    try:
                        value.append(chr(int(digits, 16)))
                        index = close
                    except (ValueError, OverflowError):
                        value.append(escaped)
                elif index + 4 < len(source):
                    digits = source[index + 1 : index + 5]
                    try:
                        value.append(chr(int(digits, 16)))
                        index += 4
                    except ValueError:
                        value.append(escaped)
                else:
                    value.append(escaped)
            else:
                value.append(escaped)
        index += 1

    return None, len(source)


def _tokenize_static_js(source):
    tokens = []
    index = 0

    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue

        if char == "/" and index + 1 < len(source):
            next_char = source[index + 1]
            if next_char == "/":
                newline = source.find("\n", index + 2)
                index = len(source) if newline == -1 else newline + 1
                continue
            if next_char == "*":
                close = source.find("*/", index + 2)
                index = len(source) if close == -1 else close + 2
                continue

        if char in "'\"`":
            value, end = _read_js_string_literal(source, index)
            tokens.append(("string", value))
            index = max(end, index + 1)
            continue

        if _is_js_identifier_start(char):
            end = index + 1
            while end < len(source) and _is_js_identifier_part(source[end]):
                end += 1
            tokens.append(("identifier", source[index:end]))
            index = end
            continue

        tokens.append(("punctuation", char))
        index += 1

    return tokens


def extract_exec_patch_arguments(source):
    """Return statically observable apply_patch arguments from an exec call.

    Recent Codex rollouts wrap nested tools in a custom `exec` call. Its input
    is JavaScript such as `const patch = "..."; tools.apply_patch(patch)`.
    Parse only static string assignments/calls; never evaluate rollout code.
    """
    if (
        not isinstance(source, str)
        or "tools" not in source
        or "apply_patch" not in source
    ):
        return []

    tokens = _tokenize_static_js(source)
    static_strings = {}
    patch_arguments = []

    for index, token in enumerate(tokens):
        if (
            token[0] == "identifier"
            and index + 2 < len(tokens)
            and tokens[index + 1] == ("punctuation", "=")
        ):
            assigned = tokens[index + 2]
            if assigned[0] == "string" and assigned[1] is not None:
                static_strings[token[1]] = assigned[1]
            else:
                static_strings.pop(token[1], None)

        if index + 4 >= len(tokens):
            continue
        if not (
            token == ("identifier", "tools")
            and tokens[index + 1] == ("punctuation", ".")
            and tokens[index + 2] == ("identifier", "apply_patch")
            and tokens[index + 3] == ("punctuation", "(")
        ):
            continue

        argument = tokens[index + 4]
        if argument[0] == "string":
            patch_text = argument[1]
        elif argument[0] == "identifier":
            patch_text = static_strings.get(argument[1])
        else:
            patch_text = None

        if not patch_text:
            continue
        patch_start = patch_text.find("*** Begin Patch")
        patch_end = patch_text.rfind("*** End Patch")
        if patch_start == -1 or patch_end < patch_start:
            continue
        patch_end += len("*** End Patch")
        patch_arguments.append(patch_text[patch_start:patch_end])

    return patch_arguments


def finalize_days(days, max_minutes, include_tombstones=False):
    results = {}
    for date_str, data in days.items():
        events = sorted(data["events"], key=lambda e: e[1])
        if not events:
            message_count = data["user_msgs"] + data["assistant_msgs"]
            if message_count > 0 or (include_tombstones and data.get("tombstone")):
                results[date_str] = {
                    "coding_time_mins": 0,
                    "ai_time_mins": 0,
                    "tokens_used": 0,
                    "provider_total_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "thoughts_tokens": 0,
                    "tool_tokens": 0,
                    "lines_changed": 0,
                    "lines_added": 0,
                    "lines_removed": 0,
                    "messages": message_count,
                    "assistant_messages": data["assistant_msgs"],
                    "projects": len(data["projects"]),
                    "tool_calls": 0,
                    "tool_breakdown": [],
                    "files_touched": 0,
                    "first_event_at": None,
                    "last_event_at": None,
                    "engaged_windows": [],
                    **measurement_metadata(data),
                }
            continue

        windows = build_engaged_windows(events, CODEX_IDLE_GAP_SECS, SESSION_TAIL_SECS)
        active_seconds = int(sum(end - start for start, end in windows))
        coding_time_mins = clamp_minutes(active_seconds, max_minutes)
        ai_time_mins = (
            max(1, int(data["output_tokens"] / HUMAN_TOKENS_PER_MIN))
            if data["output_tokens"] > 0
            else 0
        )

        results[date_str] = {
            "coding_time_mins": coding_time_mins,
            "ai_time_mins": ai_time_mins,
            "tokens_used": data["input_tokens"] + data["output_tokens"],
            "provider_total_tokens": data["input_tokens"] + data["output_tokens"],
            "input_tokens": data["input_tokens"],
            "output_tokens": data["output_tokens"],
            "cache_read_tokens": data["cache_read_tokens"],
            "cache_creation_tokens": data["cache_creation_tokens"],
            "thoughts_tokens": 0,
            "tool_tokens": 0,
            "lines_changed": data["lines_added"] - data["lines_removed"],
            "lines_added": data["lines_added"],
            "lines_removed": data["lines_removed"],
            "messages": data["user_msgs"] + data["assistant_msgs"],
            "assistant_messages": data["assistant_msgs"],
            "projects": len(data["projects"]),
            "tool_calls": data["tool_calls"],
            "tool_breakdown": build_tool_breakdown(data["tool_counts"]),
            "files_touched": len(data["files_touched"]),
            "first_event_at": events[0][1].isoformat(),
            "last_event_at": events[-1][1].isoformat(),
            "engaged_windows": windows_to_payload(windows),
            **measurement_metadata(data),
        }

    return results


def parse_session_events(session_file):
    days = defaultdict(new_day)
    session_id = os.path.splitext(os.path.basename(session_file))[0]
    if session_id.endswith(".jsonl"):
        session_id = session_id[:-6]

    session_cwd = ""
    saw_session_meta = False
    entries = load_session_entries(session_file)
    primary_metadata = primary_session_metadata(entries)
    session_id = primary_metadata.get("id") or session_id
    skip_before_line, token_baseline = detect_replay_prefix(entries)
    lineage_plan = session_lineage_plan(entries, skip_before_line)
    prev_input_tokens, prev_output_tokens, prev_cached_input_tokens = token_baseline
    v2_prev_total = token_baseline if any(token_baseline) else None

    message_candidates = extract_message_candidates(
        entries, session_id, skip_before_line
    )
    for message in message_candidates:
        message_ts = message["timestamp"]
        message_day = days[message_ts.astimezone().strftime("%Y-%m-%d")]
        if message["role"] == "user":
            message_day["user_msgs"] += 1
        else:
            message_day["assistant_msgs"] += 1
        # Preserve the historical direct-event activity semantics independently
        # from message identity. When a canonical item wins mirror deduplication,
        # its matched legacy record still supplies the original activity event.
        legacy_activity = message.get("legacy_activity")
        if message["source"] == "legacy":
            activity_role = message["role"]
            activity_ts = message_ts
        elif isinstance(legacy_activity, dict):
            activity_role = legacy_activity["role"]
            activity_ts = legacy_activity["timestamp"]
        else:
            activity_role = None
            activity_ts = None
        if activity_role and activity_ts:
            event_type = (
                "user_message" if activity_role == "user" else "assistant_message"
            )
            activity_day = days[activity_ts.astimezone().strftime("%Y-%m-%d")]
            activity_day["events"].append((event_type, activity_ts))

    if skip_before_line == sys.maxsize and entries:
        _, first_entry, first_ts = entries[0]
        date_key = first_ts.astimezone().strftime("%Y-%m-%d")
        day = days[date_key]
        day["tombstone"] = True
        payload = first_entry.get("payload", {})
        if first_entry.get("type") == "session_meta":
            session_id = payload.get("id") or session_id
            session_cwd = payload.get("cwd") or session_cwd
        if session_cwd:
            day["projects"].add(session_cwd)

        # Preserve the existing V1 behavior (the replay file contributes no
        # ranking counters) while making the V2 uncertainty observable. With
        # no replay gap there is no defensible local-history boundary, so the
        # cumulative counter deltas are unattributed and effective stays NULL.
        unresolved_previous_total = None
        for _, unresolved_entry, unresolved_ts in entries:
            usage = token_usage_from_entry(unresolved_entry)
            if usage is None:
                continue
            unresolved_day = days[
                unresolved_ts.astimezone().strftime("%Y-%m-%d")
            ]
            unresolved_day["tombstone"] = True
            unresolved_day["has_unresolved_lineage"] = True
            _, unattributed, unresolved_previous_total, method, new_epoch = (
                derive_unattributed_usage(
                    usage,
                    unresolved_previous_total,
                    "two_meta_no_gap_unresolved",
                )
            )
            unresolved_day["unattributed_input_tokens"] += unattributed[0]
            unresolved_day["unattributed_output_tokens"] += unattributed[1]
            unresolved_day["unattributed_cache_read_tokens"] += unattributed[2]
            unresolved_day["accounting_methods"].add(method)
            if unresolved_day["token_event_count"] == 0 or new_epoch:
                unresolved_day["counter_epoch_count"] += 1
            unresolved_day["token_event_count"] += 1

    for line_number, entry, ts in entries:
            if line_number < skip_before_line:
                payload = entry.get("payload", {})
                if entry.get("type") == "session_meta":
                    if not saw_session_meta:
                        session_id = payload.get("id") or session_id
                        saw_session_meta = True
                    session_cwd = payload.get("cwd") or session_cwd
                elif not session_cwd:
                    session_cwd = payload.get("cwd") or entry.get("cwd") or session_cwd
                continue

            date_key = ts.astimezone().strftime("%Y-%m-%d")
            day = days[date_key]

            if not session_cwd:
                payload = entry.get("payload", {})
                session_cwd = (
                    payload.get("cwd")
                    or entry.get("cwd")
                    or session_cwd
                )
                if session_cwd:
                    day["projects"].add(session_cwd)

            entry_type = entry.get("type")
            payload = entry.get("payload", {})

            if entry_type == "session_meta":
                if not saw_session_meta:
                    session_id = payload.get("id") or session_id
                    saw_session_meta = True
                if payload.get("cwd"):
                    session_cwd = payload["cwd"]
                    day["projects"].add(session_cwd)
                continue

            if entry_type == "turn_context":
                if payload.get("cwd"):
                    session_cwd = payload["cwd"]
                    day["projects"].add(session_cwd)
                continue

            if entry_type == "event_msg":
                payload_type = payload.get("type")
                if payload_type == "user_message":
                    # Message counting is handled by extract_message_candidates,
                    # but preserve the legacy project-attribution side effect.
                    if session_cwd:
                        day["projects"].add(session_cwd)
                elif payload_type == "token_count":
                    usage = token_usage_from_entry(entry)
                    if usage is None:
                        # Codex emits token_count records with info:null while
                        # usage is unavailable. They are activity signals, not
                        # counter resets or zero-valued measurement epochs.
                        day["events"].append(("token_count", ts))
                        continue
                    total_input, total_output, total_cached = usage["total"]
                    delta_input = max(0, total_input - prev_input_tokens)
                    delta_output = max(0, total_output - prev_output_tokens)
                    delta_cached = max(0, total_cached - prev_cached_input_tokens)
                    prev_input_tokens = max(prev_input_tokens, total_input)
                    prev_output_tokens = max(prev_output_tokens, total_output)
                    prev_cached_input_tokens = max(prev_cached_input_tokens, total_cached)
                    day["input_tokens"] += delta_input
                    day["output_tokens"] += delta_output
                    day["cache_read_tokens"] += delta_cached

                    lineage_mode = lineage_plan["mode"]
                    if lineage_mode == "ordinal_boundary":
                        ordinal = rollout_ordinal(entry)
                        if ordinal < lineage_plan["boundary"]:
                            effective, unattributed, v2_prev_total, method, new_epoch = (
                                derive_unattributed_usage(
                                    usage, v2_prev_total, "inherited_history"
                                )
                            )
                        else:
                            effective, unattributed, v2_prev_total, method, new_epoch = (
                                derive_v2_usage(usage, v2_prev_total)
                            )
                    elif lineage_mode == "unresolved_lineage":
                        day["has_unresolved_lineage"] = True
                        effective, unattributed, v2_prev_total, method, new_epoch = (
                            derive_unattributed_usage(
                                usage,
                                v2_prev_total,
                                lineage_plan.get("method", "unresolved_lineage"),
                            )
                        )
                    else:
                        effective, unattributed, v2_prev_total, method, new_epoch = (
                            derive_v2_usage(usage, v2_prev_total)
                        )
                    if lineage_mode in (
                        "ordinal_boundary",
                        "legacy_gap_boundary",
                        "referenced_history",
                    ):
                        day["accounting_methods"].add(lineage_mode)
                    day["effective_input_tokens"] += effective[0]
                    day["effective_output_tokens"] += effective[1]
                    day["effective_cache_read_tokens"] += effective[2]
                    day["unattributed_input_tokens"] += unattributed[0]
                    day["unattributed_output_tokens"] += unattributed[1]
                    day["unattributed_cache_read_tokens"] += unattributed[2]
                    day["accounting_methods"].add(method)
                    if day["token_event_count"] == 0 or new_epoch:
                        day["counter_epoch_count"] += 1
                    day["token_event_count"] += 1
                    day["events"].append(("token_count", ts))
                continue

            if entry_type != "response_item":
                continue

            payload_type = payload.get("type", "")

            # custom_tool_call: apply_patch lives here in Codex
            if payload_type == "custom_tool_call":
                day["events"].append(("tool_call", ts))
                day["tool_calls"] += 1
                name = payload.get("name", "")
                if name:
                    day["tool_counts"][name] += 1
                if name == "apply_patch":
                    patch_text = payload.get("input") or ""
                    lines_added, lines_removed, files_touched = count_patch_lines(patch_text)
                    day["lines_added"] += lines_added
                    day["lines_removed"] += lines_removed
                    day["files_touched"].update(files_touched)
                elif name == "exec":
                    for patch_text in extract_exec_patch_arguments(
                        payload.get("input") or ""
                    ):
                        lines_added, lines_removed, files_touched = count_patch_lines(
                            patch_text
                        )
                        day["lines_added"] += lines_added
                        day["lines_removed"] += lines_removed
                        day["files_touched"].update(files_touched)
                continue

            if payload_type != "function_call":
                continue

            day["events"].append(("tool_call", ts))
            day["tool_calls"] += 1

            name = payload.get("name", "")
            if name:
                day["tool_counts"][name] += 1
            arguments = payload.get("arguments") or ""

            if name == "apply_patch":
                lines_added, lines_removed, files_touched = count_patch_lines(arguments)
                day["lines_added"] += lines_added
                day["lines_removed"] += lines_removed
                day["files_touched"].update(files_touched)
                continue

            try:
                parsed_args = json.loads(arguments)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_args = {}

            workdir = parsed_args.get("workdir", "")
            if workdir:
                day["projects"].add(workdir)

    return session_id, days


def parse_session(session_file):
    session_id, days = parse_session_events(session_file)
    return session_id, finalize_days(
        days,
        MAX_MINS_PER_SESSION,
        include_tombstones=True,
    )


def summary_mode(session_dir, verbose=False):
    all_sessions = []
    merged_days = defaultdict(new_day)
    scanned = 0

    for session_file in iter_session_files(session_dir):
        scanned += 1
        try:
            session_id, day_data = parse_session_events(session_file)
        except Exception as error:
            if verbose:
                log_sync(f"failed to parse {session_file}: {error}")
            continue

        for date_str, data in day_data.items():
            events = sorted(data["events"], key=lambda e: e[1])
            message_count = data["user_msgs"] + data["assistant_msgs"]
            if not events and message_count == 0:
                continue

            session_windows = (
                build_engaged_windows(
                    events, CODEX_IDLE_GAP_SECS, SESSION_TAIL_SECS
                )
                if events
                else []
            )
            session_active = int(sum(end - start for start, end in session_windows))
            session_coding_mins = clamp_minutes(session_active, MAX_MINS_PER_SESSION)
            session_ai_mins = (
                max(1, int(data["output_tokens"] / HUMAN_TOKENS_PER_MIN))
                if data["output_tokens"] > 0
                else 0
            )

            session_entry = {
                "date": date_str,
                "session_id": f"codex:{session_id}",
                "collector_version": __version__,
                "coding_time_mins": session_coding_mins,
                "ai_time_mins": session_ai_mins,
                "tokens_used": data["input_tokens"] + data["output_tokens"],
                "provider_total_tokens": data["input_tokens"] + data["output_tokens"],
                "input_tokens": data["input_tokens"],
                "output_tokens": data["output_tokens"],
                "cache_read_tokens": data["cache_read_tokens"],
                "cache_creation_tokens": data["cache_creation_tokens"],
                "thoughts_tokens": 0,
                "tool_tokens": 0,
                "lines_changed": data["lines_added"] - data["lines_removed"],
                "lines_added": data["lines_added"],
                "lines_removed": data["lines_removed"],
                "messages": message_count,
                "assistant_messages": data["assistant_msgs"],
                "projects": len(data["projects"]),
                "tool_calls": data["tool_calls"],
                "tool_breakdown": build_tool_breakdown(data["tool_counts"]),
                "files_touched": len(data["files_touched"]),
                "first_event_at": events[0][1].isoformat() if events else None,
                "last_event_at": events[-1][1].isoformat() if events else None,
                "engaged_windows": windows_to_payload(session_windows),
                **measurement_metadata(data),
            }
            all_sessions.append(session_entry)

            merged = merged_days[date_str]
            merged["events"].extend(events)
            merged["input_tokens"] += data["input_tokens"]
            merged["output_tokens"] += data["output_tokens"]
            merged["cache_read_tokens"] += data["cache_read_tokens"]
            merged["cache_creation_tokens"] += data["cache_creation_tokens"]
            merged["effective_input_tokens"] += data["effective_input_tokens"]
            merged["effective_output_tokens"] += data["effective_output_tokens"]
            merged["effective_cache_read_tokens"] += data["effective_cache_read_tokens"]
            merged["unattributed_input_tokens"] += data["unattributed_input_tokens"]
            merged["unattributed_output_tokens"] += data["unattributed_output_tokens"]
            merged["unattributed_cache_read_tokens"] += data[
                "unattributed_cache_read_tokens"
            ]
            merged["accounting_methods"].update(data["accounting_methods"])
            merged["token_event_count"] += data["token_event_count"]
            merged["counter_epoch_count"] += data["counter_epoch_count"]
            merged["has_unresolved_lineage"] = (
                merged["has_unresolved_lineage"]
                or data["has_unresolved_lineage"]
            )
            merged["user_msgs"] += data["user_msgs"]
            merged["assistant_msgs"] += data["assistant_msgs"]
            merged["lines_added"] += data["lines_added"]
            merged["lines_removed"] += data["lines_removed"]
            merged["projects"].update(data["projects"])
            merged["tool_calls"] += data["tool_calls"]
            for tool_name, count in data["tool_counts"].items():
                merged["tool_counts"][tool_name] += count
            merged["files_touched"].update(data["files_touched"])

    daily = finalize_days(merged_days, MAX_MINS_PER_DAY)
    totals = {
        "total_coding_mins": 0,
        "total_ai_mins": 0,
        "total_tokens": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_lines_changed": 0,
        "total_lines_added": 0,
        "total_lines_removed": 0,
        "total_sessions": len(all_sessions),
        "total_messages": 0,
        "total_assistant_messages": 0,
        "total_tool_calls": 0,
        "total_files_touched": 0,
        "total_days": len(daily),
    }

    for stats in daily.values():
        totals["total_coding_mins"] += stats["coding_time_mins"]
        totals["total_ai_mins"] += stats["ai_time_mins"]
        totals["total_tokens"] += stats["tokens_used"]
        totals["total_input_tokens"] += stats["input_tokens"]
        totals["total_output_tokens"] += stats["output_tokens"]
        totals["total_lines_changed"] += stats["lines_changed"]
        totals["total_lines_added"] += stats["lines_added"]
        totals["total_lines_removed"] += stats["lines_removed"]
        totals["total_messages"] += stats["messages"]
        totals["total_assistant_messages"] += stats["assistant_messages"]
        totals["total_tool_calls"] += stats["tool_calls"]
        totals["total_files_touched"] += stats["files_touched"]

    if verbose:
        log_sync(
            f"summary complete: scanned={scanned} sessions={len(all_sessions)} days={len(daily)}"
        )
    emit_json({"summary": totals, "sessions": all_sessions, "daily": daily})


def policy_era_for_date(date_str):
    if not date_str:
        return "codex_provider_current"
    if date_str < CODEX_PROVIDER_TOTAL_POLICY_START_DATE:
        return "codex_provider_legacy_pre_2026_04_21"
    return "codex_provider_current_input_plus_output"


def iter_dates(start_date, end_date):
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        start, end = end, start
    current = start
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def build_diagnose_payload(session_dir, dates):
    target_dates = set(dates)
    scanned = 0
    matched_sessions = 0
    by_date = {
        date_str: {
            "date": date_str,
            "policy_era": policy_era_for_date(date_str),
            "sessions": 0,
            "provider_total_tokens": 0,
            "tokens_used": 0,
            "legacy_tokens_plus_cache": 0,
            "double_count_if_cache_added": 0,
            "non_cache_total": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "negative_non_cache": False,
        }
        for date_str in sorted(target_dates)
    }

    for session_file in iter_session_files(session_dir):
        scanned += 1
        try:
            session_id, day_stats = parse_session(session_file)
        except Exception as error:
            continue

        for date_str, stats in day_stats.items():
            if date_str not in target_dates:
                continue
            matched_sessions += 1
            row = by_date[date_str]
            provider_total = stats.get("provider_total_tokens", 0) or 0
            cache_total = (
                (stats.get("cache_read_tokens", 0) or 0)
                + (stats.get("cache_creation_tokens", 0) or 0)
            )
            non_cache = provider_total - cache_total
            row["sessions"] += 1
            row["provider_total_tokens"] += provider_total
            row["tokens_used"] += stats.get("tokens_used", 0) or 0
            row["legacy_tokens_plus_cache"] += (stats.get("tokens_used", 0) or 0) + cache_total
            row["double_count_if_cache_added"] += provider_total + cache_total
            row["non_cache_total"] += non_cache
            row["input_tokens"] += stats.get("input_tokens", 0) or 0
            row["output_tokens"] += stats.get("output_tokens", 0) or 0
            row["cache_read_tokens"] += stats.get("cache_read_tokens", 0) or 0
            row["cache_creation_tokens"] += stats.get("cache_creation_tokens", 0) or 0
            if non_cache < 0:
                row["negative_non_cache"] = True

    return {
        "source": "codex",
        "collector_version": __version__,
        "session_dir": session_dir,
        "scan_roots": list(iter_session_roots(session_dir)),
        "env": {
            "CODEX_HOME": os.environ.get("CODEX_HOME", ""),
            "HOME": os.path.expanduser("~"),
        },
        "files_scanned": scanned,
        "matched_sessions": matched_sessions,
        "dates": [by_date[date_str] for date_str in sorted(by_date)],
    }


def diagnose_mode(session_dir, dates):
    emit_json(build_diagnose_payload(session_dir, dates))


def load_config():
    config_path = os.path.join(AGENTBOARD_DIR, "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return None

    api = config.get("api", "")
    if not api:
        return None

    token = config.get("token", "")
    if not token:
        config = recover_token_from_claim(config_path, config)
        token = config.get("token", "")
    if not token:
        return None

    return {
        "token": token,
        "api": api,
        "device_name": config.get("device_name") or detect_device_name(),
        "platform": config.get("platform") or detect_platform_name(),
        "codex_sessions_path": config.get("codex_sessions_path", ""),
        "logs_dir": config.get("logs_dir", LOG_DIR),
    }


def resolve_session_dir(cli_value, config):
    candidates = []
    if cli_value:
        candidates.append(cli_value)
    if config.get("codex_sessions_path"):
        candidates.append(config["codex_sessions_path"])

    home = os.path.expanduser("~")
    codex_home = os.environ.get("CODEX_HOME", "")
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    candidates.extend(
        [
            os.path.join(codex_home, "sessions") if codex_home else "",
            os.path.join(home, ".codex", "sessions"),
            os.path.join(appdata, "codex", "sessions") if appdata else "",
            os.path.join(localappdata, "codex", "sessions") if localappdata else "",
        ]
    )

    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        normalized = os.path.abspath(os.path.expanduser(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        if os.path.isdir(normalized):
            return normalized

    return ""


def recover_token_from_claim(config_path, config):
    claim_code = config.get("claim_code", "")
    api = config.get("api", "")
    if not claim_code or not api:
        return config

    api_base = api.replace("/api/checkin", "")
    try:
        req = urllib.request.Request(
            f"{api_base}/api/claim/{claim_code}/status",
            headers={"User-Agent": "AgentBoard-CLI/1.0"},
        )
        response = urllib.request.urlopen(req, timeout=5, context=SSL_CONTEXT)
        data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return config

    token = data.get("token", "") if data.get("status") == "claimed" else ""
    if not token:
        return config

    config["token"] = token
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass
    return config


def post_session(config, session_entry, full_rescan=False):
    payload = json.dumps(
        {
            "token": config["token"],
            "source": "codex",
            "device_name": config.get("device_name", ""),
            "platform": config.get("platform", ""),
            "full_rescan": full_rescan,
            "session_id": session_entry["session_id"],
            "date": session_entry["date"],
            "coding_time_mins": session_entry["coding_time_mins"],
            "ai_time_mins": session_entry["ai_time_mins"],
            "tokens_used": session_entry["tokens_used"],
            "provider_total_tokens": session_entry.get("provider_total_tokens", 0),
            "input_tokens": session_entry["input_tokens"],
            "output_tokens": session_entry["output_tokens"],
            "cache_read_tokens": session_entry.get("cache_read_tokens", 0),
            "cache_creation_tokens": session_entry.get("cache_creation_tokens", 0),
            "thoughts_tokens": session_entry.get("thoughts_tokens", 0),
            "tool_tokens": session_entry.get("tool_tokens", 0),
            "lines_changed": session_entry["lines_changed"],
            "lines_added": session_entry["lines_added"],
            "lines_removed": session_entry["lines_removed"],
            "sessions": 1,
            "messages": session_entry["messages"],
            "assistant_messages": session_entry["assistant_messages"],
            "projects": session_entry["projects"],
            "tool_calls": session_entry["tool_calls"],
            "tool_breakdown": session_entry.get("tool_breakdown", []),
            "files_touched": session_entry["files_touched"],
            "first_event_at": session_entry["first_event_at"],
            "last_event_at": session_entry["last_event_at"],
            "engaged_windows": session_entry["engaged_windows"],
            "collector_version": __version__,
            "measurement_version": session_entry.get("measurement_version"),
            "measurement_revision": session_entry.get("measurement_revision"),
            "measurement_status": session_entry.get("measurement_status"),
            "measurement_digest": session_entry.get("measurement_digest"),
            "accounting_method": session_entry.get("accounting_method"),
            "effective_provider_total_tokens": session_entry.get(
                "effective_provider_total_tokens", 0
            ),
            "effective_input_tokens": session_entry.get("effective_input_tokens", 0),
            "effective_output_tokens": session_entry.get("effective_output_tokens", 0),
            "effective_cache_read_tokens": session_entry.get(
                "effective_cache_read_tokens", 0
            ),
            "effective_ai_time_mins": session_entry.get("effective_ai_time_mins"),
            "unattributed_tokens": session_entry.get("unattributed_tokens", 0),
            "unattributed_input_tokens": session_entry.get(
                "unattributed_input_tokens", 0
            ),
            "unattributed_output_tokens": session_entry.get(
                "unattributed_output_tokens", 0
            ),
            "unattributed_cache_read_tokens": session_entry.get(
                "unattributed_cache_read_tokens", 0
            ),
            "token_event_count": session_entry.get("token_event_count", 0),
            "counter_epoch_count": session_entry.get("counter_epoch_count", 0),
        }
    )

    req = urllib.request.Request(
        config["api"],
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "AgentBoard-CLI/1.0"},
        method="POST",
    )
    try:
        response = urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT)
        body = response.read().decode("utf-8", errors="replace")
        if not body:
            return {}
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as error:
        body = ""
        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        message = f"HTTP {error.code}: {error.reason}"
        if body:
            message += f" body={body}"
        raise RuntimeError(message) from error


def load_sync_state():
    state_path = os.path.join(AGENTBOARD_DIR, f"codex-sync-state.{HOST_ID}.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            raw_state = json.load(f)
    except Exception:
        return state_path, {}, False

    if not isinstance(raw_state, dict):
        return state_path, {}, False

    if raw_state.get("_collector_version") != CODEX_SYNC_STATE_VERSION:
        return state_path, {}, True

    files = raw_state.get("files")
    if isinstance(files, dict):
        return state_path, files, False
    return state_path, raw_state, False


def save_sync_state(state_path, state):
    state_dir = os.path.dirname(state_path)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(
            {"_collector_version": CODEX_SYNC_STATE_VERSION, "files": state},
            f,
            indent=2,
            sort_keys=True,
        )


def acquire_sync_lock():
    try:
        import fcntl
    except Exception:
        return None

    os.makedirs(AGENTBOARD_DIR, exist_ok=True)
    lock_file = open(SYNC_LOCK_PATH, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return False
    return lock_file


def file_signature(path):
    stat_result = os.stat(path)
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}"


def sync_mode(session_dir, verbose=False):
    config = load_config()
    if not config:
        if verbose:
            log_sync("missing config/token/api; aborting sync")
        return {
            "status": "config_error",
            "message": "Missing config/token/api",
            "session_dir": "",
            "scanned": 0,
            "skipped": 0,
            "synced": 0,
            "errors": 1,
        }

    resolved_session_dir = resolve_session_dir(session_dir, config)
    if not resolved_session_dir:
        if verbose:
            log_sync("session directory not found via configured fallback chain")
        return {
            "status": "no_session_dir",
            "message": "No Codex sessions directory found",
            "session_dir": "",
            "scanned": 0,
            "skipped": 0,
            "synced": 0,
            "errors": 0,
        }

    sync_lock = acquire_sync_lock()
    if sync_lock is False:
        if verbose:
            log_sync("another Codex sync is already running; skipping")
        return {
            "status": "skipped",
            "message": "Another Codex sync is already running",
            "session_dir": resolved_session_dir,
            "scanned": 0,
            "skipped": 0,
            "synced": 0,
            "errors": 0,
        }

    state_path, state, state_invalidated = load_sync_state()
    # A collector-version change must scan every file to populate V2 shadow
    # measurements, but it must not replace V1 ranking counters before the
    # shadow rollout is explicitly approved.
    full_rescan_mode = False
    if state_invalidated and verbose:
        log_sync("sync state invalidated; running a shadow-only full scan")
    next_state = {}
    scanned = 0
    skipped = 0
    synced = 0
    errors = 0

    for session_file in iter_session_files(resolved_session_dir):
        scanned += 1
        try:
            signature = file_signature(session_file)
        except OSError as error:
            if verbose:
                log_sync(f"failed to stat {session_file}: {error}")
            continue

        if not state_invalidated and state.get(session_file) == signature:
            next_state[session_file] = signature
            skipped += 1
            continue

        shadow_settled = True
        try:
            session_id, day_stats = parse_session(session_file)
            for date_str, stats in day_stats.items():
                if verbose:
                    log_sync(
                        "posting "
                        f"{session_file} date={date_str} "
                        f"coding={stats.get('coding_time_mins', 0)} "
                        f"messages={stats.get('messages', 0)} "
                        f"tool_calls={stats.get('tool_calls', 0)} "
                        f"files_touched={stats.get('files_touched', 0)} "
                        f"tokens={stats.get('tokens_used', 0)} "
                        f"first={stats.get('first_event_at')} "
                        f"last={stats.get('last_event_at')} "
                        f"windows={len(stats.get('engaged_windows', []) or [])}"
                    )
                response = post_session(
                    config,
                    {
                        "session_id": f"codex:{session_id}",
                        "date": date_str,
                        **stats,
                    },
                    full_rescan=full_rescan_mode,
                )
                shadow_status = (
                    response.get("shadow_status")
                    if isinstance(response, dict)
                    else None
                )
                if stats.get("measurement_version") and (
                    shadow_status not in SHADOW_SETTLED_STATUSES
                ):
                    shadow_settled = False
                    if verbose:
                        log_sync(
                            "shadow measurement was not committed; "
                            f"will retry file status={shadow_status or 'missing'}"
                        )
                elif shadow_status == "rejected_older_revision" and verbose:
                    log_sync(
                        "shadow measurement skipped because the server already "
                        "has a newer revision"
                    )
                synced += 1
        except Exception as error:
            log_sync_error(f"failed to sync {session_file}", error)
            errors += 1
            continue
        if shadow_settled:
            next_state[session_file] = signature

    try:
        save_sync_state(state_path, next_state)
    except Exception as error:
        if verbose:
            log_sync(f"failed to save sync state {state_path}: {error}")
        return {
            "status": "error",
            "message": f"Failed to save sync state: {error}",
            "session_dir": resolved_session_dir,
            "scanned": scanned,
            "skipped": skipped,
            "synced": synced,
            "errors": max(1, errors),
        }

    if verbose:
        log_sync(
            f"scan complete: scanned={scanned} skipped={skipped} synced={synced} state={state_path}"
        )
    if errors == 0:
        mark_last_success()

    status = "success"
    message = "Sync complete"
    if scanned == 0:
        status = "no_sessions"
        message = "No Codex sessions found"
    elif errors > 0 and synced == 0:
        status = "error"
        message = "All sync attempts failed"
    elif errors > 0:
        status = "partial"
        message = "Sync completed with some errors"

    return {
        "status": status,
        "message": message,
        "session_dir": resolved_session_dir,
        "scanned": scanned,
        "skipped": skipped,
        "synced": synced,
        "errors": errors,
    }


def daemon_mode(session_dir):
    while True:
        try:
            sync_mode(session_dir, verbose=False)
        except Exception as error:
            log_sync_error("daemon sync iteration failed", error)
        time.sleep(SYNC_INTERVAL_SECS)


def main():
    args = sys.argv[1:]
    json_output = False
    filtered_args = []
    for value in args:
        if value == "--json":
            json_output = True
        else:
            filtered_args.append(value)
    args = filtered_args
    codex_dir = ""

    if len(args) >= 1 and args[0] == "--summary":
        config = load_config() or {}
        if len(args) >= 2:
            codex_dir = args[1]
        resolved_dir = resolve_session_dir(codex_dir, config)
        if not resolved_dir:
            log_sync("summary aborted: no Codex sessions directory found")
            emit_json({"summary": {}, "sessions": [], "daily": {}})
            return 0
        summary_mode(resolved_dir, verbose=True)
        return 0

    if len(args) >= 2 and args[0] == "--diagnose-date":
        config = load_config() or {}
        date_str = args[1]
        if len(args) >= 3:
            codex_dir = args[2]
        resolved_dir = resolve_session_dir(codex_dir, config)
        if not resolved_dir:
            log_sync("diagnose aborted: no Codex sessions directory found")
            emit_json({"source": "codex", "dates": [], "scan_roots": []})
            return 0
        diagnose_mode(resolved_dir, [date_str])
        return 0

    if len(args) >= 3 and args[0] == "--diagnose-range":
        config = load_config() or {}
        start_date = args[1]
        end_date = args[2]
        if len(args) >= 4:
            codex_dir = args[3]
        resolved_dir = resolve_session_dir(codex_dir, config)
        if not resolved_dir:
            log_sync("diagnose aborted: no Codex sessions directory found")
            emit_json({"source": "codex", "dates": [], "scan_roots": []})
            return 0
        diagnose_mode(resolved_dir, list(iter_dates(start_date, end_date)))
        return 0

    if len(args) >= 1 and args[0] == "--sync":
        if len(args) >= 2:
            codex_dir = args[1]
        result = sync_mode(codex_dir, verbose=True)
        if json_output:
            emit_json(result)
        if result["status"] in ("success", "partial", "no_sessions", "no_session_dir", "skipped"):
            return 0
        return 1

    if len(args) >= 1 and args[0] == "--daemon":
        if len(args) >= 2:
            codex_dir = args[1]
        daemon_mode(codex_dir)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
