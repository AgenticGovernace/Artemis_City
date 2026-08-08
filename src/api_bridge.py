#!/usr/bin/env python3
"""JSON command bridge between the TypeScript API and the Python core.

The TypeScript Express layer (``app/api``) is the public HTTP boundary; it
shells out to this module to perform registry / governance operations against
the authoritative Python core, exchanging JSON over stdin/stdout.

Protocol
--------
Invoked as ``python -m src.api_bridge`` (cwd = repo root). Reads a single JSON
object from stdin of the form::

    {"command": "<namespace>.<action>", "payload": { ... }}

and writes a single JSON object to stdout::

    {"ok": true, "data": { ... }}            # success
    {"ok": false, "error": "...", "code": "..."}   # failure

The registry database path is taken from ``payload.db_path`` if present,
otherwise the ``ARTEMIS_REGISTRY_DB`` env var, otherwise the package default.
Keeping this stdlib-only (no web framework) means it runs in CI and is unit
testable directly via :func:`dispatch`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Tuple, cast

from src.agents.atp.atp_context import resolve_task_context
from src.agents.atp.atp_models import ATPActionType, ATPMode, ATPPriority
from src.agents.atp.atp_parser import ATPParser
from src.agents.atp.atp_validator import ATPValidator
from src.agents.llm_agent import LLMAgent
from src.governance.approvals import SelfUpdateGovernor, UpdateProposal
from src.governance.checkpoints import CheckpointStore, RollbackManager
from src.governance.trust import TrustMetrics, compute_trust_score, trust_breakdown
from src.integration.agent_registry import AgentRegistry, AgentRegistryStore
from src.integration.hebbian_router import HebbianRouter
from src.integration.learning_governance import LearningGovernanceCoordinator
from src.integration.memory_bus import MemoryBus
from src.integration.sandbox import AgentSandbox
from src.integration.trust_interface import TRUST_THRESHOLDS, TrustInterface, TrustLevel
from src.mcp.hebbian_weights import HebbianWeightManager
from src.mcp.vector_store import LocalVectorStore
from src.obsidian_integration.manager import ObsidianManager
from src.runtime_paths import data_path
from src.utils import get_run_logger
from src.utils.run_logger import RunLogger


class BridgeError(Exception):
    """A command failure carrying a stable machine-readable code."""

    def __init__(self, message: str, code: str = "BRIDGE_ERROR"):
        super().__init__(message)
        self.code = code


def _resolve_db_path(payload: Dict[str, Any]) -> str:
    configured_path = payload.get("db_path")
    if configured_path is None:
        for sibling_key in ("trust_db_path", "hebbian_db_path"):
            sibling = payload.get(sibling_key)
            if sibling and sibling != ":memory:":
                configured_path = str(Path(str(sibling)).with_name("agent_registry.db"))
                break
    return data_path(
        "agent_registry.db",
        configured_path,
        env_var="ARTEMIS_REGISTRY_DB",
    )


def _store(payload: Dict[str, Any]) -> AgentRegistryStore:
    return AgentRegistryStore(db_path=_resolve_db_path(payload))


def _require(payload: Dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise BridgeError(f"missing required field: {key}", code="INVALID_REQUEST")
    return payload[key]


def _require_str(payload: Dict[str, Any], key: str, allow_empty: bool = False) -> str:
    value = _require(payload, key) if not allow_empty else payload.get(key)
    if value is None:
        raise BridgeError(f"missing required field: {key}", code="INVALID_REQUEST")
    if not isinstance(value, str):
        raise BridgeError(f"{key} must be a string", code="INVALID_REQUEST")
    if not allow_empty and value.strip() == "":
        raise BridgeError(f"missing required field: {key}", code="INVALID_REQUEST")
    return value


def _optional_limit(
    payload: Dict[str, Any], key: str = "limit", default: int = 10, maximum: int = 100
) -> int:
    raw_limit = payload.get(key, default)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise BridgeError(
            f"{key} must be an integer, got {raw_limit!r}", code="INVALID_REQUEST"
        )
    if limit < 1:
        raise BridgeError(f"{key} must be >= 1", code="INVALID_REQUEST")
    if limit > maximum:
        raise BridgeError(f"{key} must be <= {maximum}", code="INVALID_REQUEST")
    return limit


def _optional_float(
    payload: Dict[str, Any],
    key: str,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if key not in payload or payload[key] is None:
        return default
    try:
        value = float(payload[key])
    except (TypeError, ValueError):
        raise BridgeError(f"{key} must be a number", code="INVALID_REQUEST")
    if minimum is not None and value < minimum:
        raise BridgeError(f"{key} must be >= {minimum}", code="INVALID_REQUEST")
    if maximum is not None and value > maximum:
        raise BridgeError(f"{key} must be <= {maximum}", code="INVALID_REQUEST")
    return value


def _optional_string_list(payload: Dict[str, Any], key: str) -> list[str] | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BridgeError(f"{key} must be a list of strings", code="INVALID_REQUEST")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_note_path(path: str, allow_empty: bool = False) -> str:
    if not isinstance(path, str):
        raise BridgeError("path must be a string", code="INVALID_REQUEST")
    if path == "":
        if allow_empty:
            return ""
        raise BridgeError("missing required field: path", code="INVALID_REQUEST")

    requested = Path(path)
    if requested.is_absolute():
        raise BridgeError("path must be relative to the vault", code="INVALID_REQUEST")
    if ".." in requested.parts:
        raise BridgeError(
            "path must not contain '..' traversal", code="INVALID_REQUEST"
        )
    return requested.as_posix()


def _require_note_path(payload: Dict[str, Any], key: str = "path") -> str:
    return _safe_note_path(_require_str(payload, key), allow_empty=False)


def _optional_note_path(
    payload: Dict[str, Any], key: str = "path", default: str = ""
) -> str:
    value = payload.get(key, default)
    if value is None:
        value = default
    return _safe_note_path(value, allow_empty=True)


def _validation_to_dict(result) -> Dict[str, Any]:
    return {
        "is_valid": result.is_valid,
        "valid": result.is_valid,
        "warnings": result.warnings,
        "errors": result.errors,
        "suggestions": result.suggestions,
        "has_issues": result.has_issues,
    }


def _memory_manager(payload: Dict[str, Any]) -> ObsidianManager:
    vault_path = payload.get("vault_path") or os.environ.get("OBSIDIAN_VAULT_PATH")
    return ObsidianManager(vault_path=vault_path)


def _memory_dependencies(payload: Dict[str, Any]) -> Tuple[ObsidianManager, MemoryBus]:
    manager = _memory_manager(payload)

    vector_db_path = data_path(
        "vector_store.db",
        payload.get("vector_db_path"),
        env_var="ARTEMIS_VECTOR_DB",
    )
    search_dirs = payload.get("search_dirs")
    if search_dirs is None:
        search_dirs = [""]
    if not isinstance(search_dirs, list) or not all(
        isinstance(item, str) for item in search_dirs
    ):
        raise BridgeError("search_dirs must be a list of strings", "INVALID_REQUEST")
    search_dirs = [_safe_note_path(item, allow_empty=True) for item in search_dirs]

    vector_store = LocalVectorStore(db_path=str(vector_db_path))
    return manager, MemoryBus(manager, vector_store, search_dirs=search_dirs)


def _trust_interface(payload: Dict[str, Any]) -> TrustInterface:
    configured_path = payload.get("trust_db_path")
    if configured_path is None and payload.get("db_path"):
        configured_path = str(
            Path(str(payload["db_path"])).with_name("trust_scores.db")
        )
    db_path = data_path(
        "trust_scores.db",
        configured_path,
        env_var="ARTEMIS_TRUST_DB",
    )
    return TrustInterface(db_path=Path(db_path))


def _learning_governance(
    payload: Dict[str, Any],
    *,
    include_hebbian: bool = False,
    include_trust: bool = True,
) -> LearningGovernanceCoordinator:
    """Build the shared write-through coordinator for one bridge request."""
    return LearningGovernanceCoordinator(
        _store(payload),
        trust_interface=_trust_interface(payload) if include_trust else None,
        hebbian_manager=_hebbian_manager(payload) if include_hebbian else None,
        checkpoint_store=_checkpoint_store(payload),
    )


def _hebbian_manager(payload: Dict[str, Any]) -> HebbianWeightManager:
    configured_path = payload.get("hebbian_db_path")
    if configured_path is None and payload.get("db_path"):
        configured_path = str(
            Path(str(payload["db_path"])).with_name("hebbian_weights.db")
        )
    db_path = data_path(
        "hebbian_weights.db",
        configured_path,
        env_var="ARTEMIS_HEBBIAN_DB",
    )
    return HebbianWeightManager(db_path=str(db_path))


def _checkpoint_store(payload: Dict[str, Any]) -> CheckpointStore:
    configured_path = payload.get("checkpoint_dir")
    if configured_path is None:
        for sibling_key in ("db_path", "trust_db_path", "hebbian_db_path"):
            sibling = payload.get(sibling_key)
            if sibling and sibling != ":memory:":
                configured_path = str(Path(str(sibling)).parent / "checkpoints")
                break
    return CheckpointStore(checkpoint_dir=configured_path)


def _atp_db_path(payload: Dict[str, Any]) -> str:
    return data_path(
        "atp_messages.db",
        payload.get("atp_db_path"),
        env_var="ARTEMIS_ATP_DB",
    )


def _ensure_atp_store(payload: Dict[str, Any]) -> str:
    db_path = str(_atp_db_path(payload))
    db_dir = os.path.dirname(db_path)
    if db_path != ":memory:" and db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS atp_messages (
                message_id TEXT PRIMARY KEY,
                raw_message TEXT NOT NULL,
                parsed_json TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                status TEXT NOT NULL,
                route_json TEXT,
                response_json TEXT,
                provenance_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(atp_messages)")}
        if "provenance_id" not in existing:
            conn.execute("ALTER TABLE atp_messages ADD COLUMN provenance_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_atp_status_created "
            "ON atp_messages(status, created_at DESC)"
        )
        conn.commit()
    return db_path


def _atp_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "raw_message": row["raw_message"],
        "message": json.loads(row["parsed_json"]),
        "validation": json.loads(row["validation_json"]),
        "status": row["status"],
        "route": json.loads(row["route_json"]) if row["route_json"] else None,
        "response": json.loads(row["response_json"]) if row["response_json"] else None,
        "provenance_id": row["provenance_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _parse_and_validate_atp(raw_input: str, strict: bool = False) -> Dict[str, Any]:
    message, metrics = ATPParser().parse_with_metrics(raw_input)
    validation = ATPValidator(strict=strict).validate(message)
    return {
        "message": message.to_dict(),
        "metrics": metrics,
        "validation": _validation_to_dict(validation),
    }


def _record_atp_prompt_provenance(
    payload: Dict[str, Any],
    raw_input: str,
    parsed_message: Dict[str, Any],
    *,
    source: str,
    provenance_id: str | None = None,
) -> str:
    """Fail-closed parent provenance write for an ATP prompt."""
    parent_id = provenance_id or str(uuid.uuid4())
    try:
        return _atp_provenance_logger(payload).log_event(
            "atp_prompt_received",
            "atp",
            {
                "source": source,
                "message": parsed_message,
                "content_length": len(raw_input),
            },
            "ATP prompt accepted",
            prov_id=parent_id,
        )
    except Exception as exc:
        raise BridgeError("ATP provenance write failed", code="BRIDGE_ERROR") from exc


def _record_atp_child_provenance(
    payload: Dict[str, Any],
    parent_id: str,
    event_type: str,
    metadata: Dict[str, Any],
) -> str:
    """Fail-closed child provenance write for an ATP action."""
    try:
        return _atp_provenance_logger(payload).log_event(
            event_type,
            "atp",
            metadata,
            parent_prov_id=parent_id,
        )
    except Exception as exc:
        raise BridgeError("ATP provenance write failed", code="BRIDGE_ERROR") from exc


def _atp_provenance_logger(payload: Dict[str, Any]) -> RunLogger:
    """Use sibling test stores when a bridge request overrides runtime paths."""
    for key in ("atp_db_path", "db_path", "hebbian_db_path", "trust_db_path"):
        configured = payload.get(key)
        if configured and configured != ":memory:":
            parent = Path(str(configured)).parent
            return RunLogger(
                log_dir=str(parent / "logs"),
                db_path=str(parent / "run_logs.db"),
            )
    return get_run_logger()


# ---------------------------------------------------------------------------
# Command handlers — each takes the payload dict and returns a JSON-able dict
# ---------------------------------------------------------------------------


_LLM_MESSAGE_ROLES = {"system", "user", "assistant"}
_LLM_OPTION_KEYS = {
    "temperature",
    "maxTokens",
    "max_tokens",
    "systemPrompt",
    "system_prompt",
}


def _llm_options(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the public LLM generation options."""
    options = payload.get("options")
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise BridgeError("options must be an object", code="INVALID_REQUEST")
    unsupported = sorted(set(options) - _LLM_OPTION_KEYS)
    if unsupported:
        raise BridgeError(
            "unsupported LLM option(s): " + ", ".join(unsupported),
            code="INVALID_REQUEST",
        )
    return options


def _llm_number_option(
    payload: Dict[str, Any],
    options: Dict[str, Any],
    *,
    option_key: str,
    aliases: tuple[str, ...] = (),
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Read one finite numeric LLM option from nested or flat payloads."""
    raw: Any = default
    for key in (option_key, *aliases):
        if key in options:
            raw = options[key]
            break
        if key in payload:
            raw = payload[key]
            break
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise BridgeError(f"{option_key} must be a number", code="INVALID_REQUEST")
    value = float(raw)
    if not (minimum <= value <= maximum):
        raise BridgeError(
            f"{option_key} must be between {minimum} and {maximum}",
            code="INVALID_REQUEST",
        )
    return value


def _llm_max_tokens(payload: Dict[str, Any], options: Dict[str, Any]) -> int:
    """Read a bounded integral max-token value."""
    raw: Any = 4000
    for key in ("maxTokens", "max_tokens"):
        if key in options:
            raw = options[key]
            break
        if key in payload:
            raw = payload[key]
            break
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not float(raw).is_integer()
    ):
        raise BridgeError("maxTokens must be an integer", code="INVALID_REQUEST")
    value = int(raw)
    if not 1 <= value <= 65536:
        raise BridgeError(
            "maxTokens must be between 1 and 65536", code="INVALID_REQUEST"
        )
    return value


def _llm_model(payload: Dict[str, Any]) -> str | None:
    """Validate an optional caller-selected model identifier."""
    model = payload.get("model")
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise BridgeError("model must be a non-empty string", code="INVALID_REQUEST")
    return model.strip()


def _llm_system_prompt(payload: Dict[str, Any], options: Dict[str, Any]) -> str | None:
    """Validate an optional system prompt without accepting arbitrary options."""
    value = None
    for key in ("systemPrompt", "system_prompt"):
        if key in options:
            value = options[key]
            break
        if key in payload:
            value = payload[key]
            break
    if value is None:
        return None
    if not isinstance(value, str):
        raise BridgeError("systemPrompt must be a string", code="INVALID_REQUEST")
    return value.strip() or None


def _llm_context(payload: Dict[str, Any], *, prompt: str | None = None) -> dict:
    """Build the narrow task context accepted by the authoritative LLM agent."""
    options = _llm_options(payload)
    context: dict[str, Any] = {
        "temperature": _llm_number_option(
            payload,
            options,
            option_key="temperature",
            default=0.2,
            minimum=0.0,
            maximum=2.0,
        ),
        "max_tokens": _llm_max_tokens(payload, options),
    }
    model = _llm_model(payload)
    if model is not None:
        context["model"] = model
    system_prompt = _llm_system_prompt(payload, options)
    if system_prompt is not None:
        context["system_prompt"] = system_prompt
    if prompt is not None:
        context["prompt"] = prompt
    return context


def _llm_messages(payload: Dict[str, Any]) -> list[dict[str, str]]:
    """Validate chat messages before any provider request is attempted."""
    messages = _require(payload, "messages")
    if not isinstance(messages, list) or not messages:
        raise BridgeError("messages must be a non-empty array", code="INVALID_REQUEST")
    normalized: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise BridgeError(
                f"messages[{index}] must be an object", code="INVALID_REQUEST"
            )
        role = message.get("role")
        content = message.get("content")
        if role not in _LLM_MESSAGE_ROLES:
            raise BridgeError(
                f"messages[{index}].role must be one of: "
                + ", ".join(sorted(_LLM_MESSAGE_ROLES)),
                code="INVALID_REQUEST",
            )
        if not isinstance(content, str) or not content.strip():
            raise BridgeError(
                f"messages[{index}].content must be a non-empty string",
                code="INVALID_REQUEST",
            )
        normalized.append({"role": role, "content": content})
    return normalized


def _llm_failure_code(result: Dict[str, Any]) -> str:
    """Map provider evidence to a stable bridge-level failure code."""
    exo_request = result.get("exo_request")
    if not isinstance(exo_request, dict):
        exo_request = {}
    status = result.get("upstream_status_code", exo_request.get("http_status"))
    failure_kind = str(
        result.get("failure_kind") or exo_request.get("failure_kind") or ""
    )
    if status == 429 or failure_kind == "rate_limited":
        return "RATE_LIMITED"
    if status == 504 or failure_kind == "read_timeout":
        return "TIMEOUT"
    if status in {502, 503} or failure_kind == "connect_timeout":
        return "SERVICE_UNAVAILABLE"
    return "PROVIDER_ERROR"


def _normalize_llm_result(result: Any, agent: LLMAgent) -> Dict[str, Any]:
    """Preserve the agent result while exposing stable public response fields."""
    if not isinstance(result, dict):
        raise BridgeError("LLM agent returned an invalid result", code="BRIDGE_ERROR")
    normalized = dict(result)
    status = normalized.get("status")
    if status not in {"success", "failed"}:
        raise BridgeError("LLM agent returned an invalid status", code="BRIDGE_ERROR")
    summary = normalized.get("summary")
    if not isinstance(summary, str):
        raise BridgeError("LLM agent returned an invalid summary", code="BRIDGE_ERROR")

    raw_output = normalized.get("raw_output")
    normalized["raw"] = raw_output if isinstance(raw_output, str) else None
    normalized.setdefault("provider", None)
    normalized.setdefault("fallback_used", False)
    normalized.setdefault("model", agent.model_id)
    normalized.setdefault("exo_request", None)
    normalized.setdefault("error", None if status == "success" else summary)
    if status == "failed":
        normalized["bridge_code"] = _llm_failure_code(normalized)
    return normalized


def _run_llm(context: Dict[str, Any]) -> Dict[str, Any]:
    """Run one real Exo request after the agent's sandbox preflight."""
    agent = LLMAgent()
    sandbox = AgentSandbox(
        agent.name,
        policies=agent.get_sandbox_policies(),
        capabilities=agent.capabilities,
    )
    dispatch_result = sandbox.check_dispatch("llm_chat")
    if not dispatch_result.allowed:
        raise BridgeError(
            dispatch_result.reason or "LLM dispatch denied", code="FORBIDDEN"
        )
    for action in agent.get_sandbox_actions(context):
        action_result = sandbox.check_action(
            str(action.get("tool_name", "")),
            path=action.get("path"),
            operation=action.get("operation"),
        )
        if not action_result.allowed:
            raise BridgeError(
                action_result.reason or "LLM action denied", code="FORBIDDEN"
            )
    return _normalize_llm_result(agent.perform_task(context), agent)


def _llm_chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a real Exo chat request through :class:`LLMAgent`."""
    context = _llm_context(payload)
    messages = _llm_messages(payload)
    system_prompt = context.pop("system_prompt", None)
    if system_prompt is not None:
        messages.insert(0, {"role": "system", "content": system_prompt})
    context["messages"] = messages
    return _run_llm(context)


def _llm_complete(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a real Exo text-completion request through :class:`LLMAgent`."""
    prompt = _require_str(payload, "prompt").strip()
    return _run_llm(_llm_context(payload, prompt=prompt))


def _llm_config(_: Dict[str, Any]) -> Dict[str, Any]:
    """Return environment-backed Exo configuration without probing the network."""
    agent = LLMAgent()
    return {
        "source": "configured",
        "default_model": agent.model_id,
        "models": [
            {
                "id": agent.model_id,
                "name": agent.model_id,
                "provider": "exo",
                "source": "configured",
                "running": None,
                "capabilities": list(agent.capabilities),
            }
        ],
        "providers": [
            {
                "id": "exo",
                "name": "Exo",
                "enabled": True,
                "base_url": agent.model_url,
                "api_key_configured": bool(agent.api_key),
                "source": "environment",
            }
        ],
    }


def _list_agents(payload: Dict[str, Any]) -> Dict[str, Any]:
    records = _store(payload).list_agent_records()
    return {"agents": records, "total": len(records)}


def _get_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require(payload, "name")
    record = _store(payload).get_agent_record(name)
    if record is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")
    return record


def _register_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = payload.get("name") or payload.get("id")
    if not isinstance(name, str) or not name.strip():
        raise BridgeError("missing required field: name", code="INVALID_REQUEST")
    name = name.strip()
    capabilities = _optional_string_list(payload, "capabilities") or []
    description = (
        payload["description"] if "description" in payload else payload.get("role")
    )
    if description is not None and not isinstance(description, str):
        raise BridgeError("description must be a string", code="INVALID_REQUEST")

    alignment = _optional_float(payload, "alignment", 0.5, 0.0, 1.0)
    accuracy = _optional_float(payload, "accuracy", 0.5, 0.0, 1.0)
    efficiency = _optional_float(payload, "efficiency", 0.5, 0.0, 1.0)
    trust_tier = payload.get("trust_tier", "monitored")
    if trust_tier not in ("auto", "monitored", "human"):
        raise BridgeError(
            "trust_tier must be auto, monitored, or human", "INVALID_REQUEST"
        )
    status = payload.get("status", "active")
    if status not in ("active", "suspended", "quarantined"):
        raise BridgeError(
            "status must be active, suspended, or quarantined", "INVALID_REQUEST"
        )
    trust_score = _optional_float(payload, "trust_score", None, 0.0, 1.0)
    if trust_score is None and "trustLevel" in payload:
        trust_score = _optional_float(payload, "trustLevel", None, 0.0, 1.0)

    store = _store(payload)
    now = time.time()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO agents (
                name, capabilities, description, alignment, accuracy, efficiency,
                created_at, updated_at, trust_tier, status, trust_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                capabilities = excluded.capabilities,
                description = excluded.description,
                alignment = excluded.alignment,
                accuracy = excluded.accuracy,
                efficiency = excluded.efficiency,
                updated_at = excluded.updated_at,
                trust_tier = excluded.trust_tier,
                status = excluded.status,
                trust_score = excluded.trust_score
            """,
            (
                name,
                json.dumps(capabilities),
                description,
                alignment,
                accuracy,
                efficiency,
                now,
                now,
                trust_tier,
                status,
                trust_score,
            ),
        )
        conn.commit()
    return store.get_agent_record(name) or {}


def _update_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require_str(payload, "name")
    updates = payload.get("updates", {})
    if not isinstance(updates, dict):
        raise BridgeError("updates must be an object", code="INVALID_REQUEST")
    store = _store(payload)
    record = store.get_agent_record(name)
    if record is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")

    capabilities = record["capabilities"]
    if "capabilities" in updates:
        capabilities = _optional_string_list(updates, "capabilities") or []
    description = record["description"]
    if "description" in updates or "role" in updates:
        description = (
            updates["description"] if "description" in updates else updates.get("role")
        )
        if description is not None and not isinstance(description, str):
            raise BridgeError("description must be a string", code="INVALID_REQUEST")
    scores = {key: record[key] for key in ("alignment", "accuracy", "efficiency")}
    trust_score = record["trust_score"]
    for key in ("alignment", "accuracy", "efficiency", "trust_score"):
        if key in updates:
            value = _optional_float(updates, key, None, 0.0, 1.0)
            if key == "trust_score":
                trust_score = value
            else:
                scores[key] = value
    if "trustLevel" in updates and "trust_score" not in updates:
        trust_score = _optional_float(updates, "trustLevel", None, 0.0, 1.0)
    trust_tier = record["trust_tier"]
    if "trust_tier" in updates:
        tier = updates["trust_tier"]
        if tier not in ("auto", "monitored", "human"):
            raise BridgeError(
                "trust_tier must be auto, monitored, or human", "INVALID_REQUEST"
            )
        trust_tier = tier
    status = record["status"]
    if "status" in updates:
        status = updates["status"]
        if status not in ("active", "suspended", "quarantined"):
            raise BridgeError(
                "status must be active, suspended, or quarantined", "INVALID_REQUEST"
            )

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            UPDATE agents
            SET capabilities = ?, description = ?, alignment = ?, accuracy = ?,
                efficiency = ?, trust_score = ?, trust_tier = ?, status = ?,
                updated_at = ?
            WHERE name = ?
            """,
            (
                json.dumps(capabilities),
                description,
                scores["alignment"],
                scores["accuracy"],
                scores["efficiency"],
                trust_score,
                trust_tier,
                status,
                time.time(),
                name,
            ),
        )
        conn.commit()
    return store.get_agent_record(name) or {}


def _delete_agent(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require_str(payload, "name")
    store = _store(payload)
    if store.get_agent_record(name) is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM violations WHERE agent_name = ?", (name,))
        cursor = conn.execute("DELETE FROM agents WHERE name = ?", (name,))
        conn.commit()
    return {"name": name, "deleted": cursor.rowcount > 0}


def _set_agent_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require_str(payload, "name")
    status = _require_str(payload, "status")
    if status not in ("active", "suspended", "quarantined"):
        raise BridgeError(
            "status must be active, suspended, or quarantined", "INVALID_REQUEST"
        )
    store = _store(payload)
    if store.get_agent_record(name) is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE agents SET status = ?, updated_at = ? WHERE name = ?",
            (status, time.time(), name),
        )
        conn.commit()
    record = store.get_agent_record(name) or {}
    if "reason" in payload:
        record["reason"] = payload["reason"]
    return record


def _get_violations(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require(payload, "name")
    store = _store(payload)
    if store.get_agent_record(name) is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")
    include_cleared = bool(payload.get("include_cleared", False))
    raw_limit = payload.get("limit", 100)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise BridgeError(
            f"limit must be an integer, got {raw_limit!r}", code="INVALID_REQUEST"
        )
    if limit < 1:
        raise BridgeError("limit must be >= 1", code="INVALID_REQUEST")
    violations = store.get_violations(name, include_cleared, limit)
    state = store.get_governance_state(name) or {}
    return {
        "agent_name": name,
        "violation_count": state.get("violation_count", 0),
        "quarantined": state.get("status") == "quarantined",
        "violations": violations,
    }


def _clear_violations(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require(payload, "name")
    store = _store(payload)
    if store.get_agent_record(name) is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")
    rationale = payload.get("rationale", "")
    override_tier = payload.get("override_tier")
    try:
        cleared = _learning_governance(payload).clear_violations(
            name,
            rationale,
            override_tier,
        )
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST")
    state = store.get_governance_state(name) or {}
    return {
        "agent_name": name,
        "cleared": cleared,
        "violation_count": state.get("violation_count", 0),
        "quarantined": state.get("status") == "quarantined",
        "trust_tier": state.get("trust_tier"),
    }


def _set_trust_tier(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require(payload, "name")
    tier = _require(payload, "tier")
    store = _store(payload)
    if store.get_agent_record(name) is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")
    try:
        store.set_trust_tier(name, tier)
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST")
    return store.get_governance_state(name) or {}


def _record_violation(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require(payload, "name")
    vtype = _require(payload, "violation_type")
    details = payload.get("details", {})
    store = _store(payload)
    if store.get_agent_record(name) is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")
    try:
        return _learning_governance(payload).record_violation(name, vtype, details)
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST")


def _build_metrics(payload: Dict[str, Any], store, name: str) -> TrustMetrics:
    """Construct TrustMetrics from a payload's ``metrics`` block, defaulting
    ``recent_violation_count`` to the agent's persisted count when absent."""
    metrics = dict(payload.get("metrics") or {})
    if "recent_violation_count" not in metrics:
        state = store.get_governance_state(name) or {}
        metrics["recent_violation_count"] = state.get("violation_count", 0) or 0
    # Drop unknown keys so a stray field can't crash the dataclass.
    allowed = TrustMetrics.__dataclass_fields__.keys()
    filtered = {k: v for k, v in metrics.items() if k in allowed}
    return TrustMetrics(**filtered)


def _compute_trust(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require(payload, "name")
    store = _store(payload)
    if store.get_agent_record(name) is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")
    metrics = _build_metrics(payload, store, name)
    score = compute_trust_score(metrics)
    persist = payload.get("persist", True)
    if persist:
        _learning_governance(payload).set_trust_score(name, score)
    return {
        "agent_name": name,
        "trust_score": score,
        "persisted": bool(persist),
        "breakdown": trust_breakdown(metrics),
        "has_execution_history": metrics.has_execution_history,
    }


def _evaluate_update(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _require(payload, "agent_name")
    store = _store(payload)
    record = store.get_agent_record(name)
    if record is None:
        raise BridgeError(f"agent not found: {name}", code="NOT_FOUND")
    metrics_given = payload.get("metrics") is not None
    has_history = bool(payload.get("has_history", True))

    # Resolve the trust score: explicit > computed-from-metrics > persisted.
    if "trust_score" in payload and payload["trust_score"] is not None:
        trust_score = float(payload["trust_score"])
        if not 0.0 <= trust_score <= 1.0:
            raise BridgeError(
                "trust_score must be between 0 and 1", code="INVALID_REQUEST"
            )
    elif metrics_given:
        metrics = _build_metrics(payload, store, name)
        trust_score = compute_trust_score(metrics)
        has_history = metrics.has_execution_history
    else:
        trust_score = record.get("trust_score")
        if trust_score is None:
            raise BridgeError(
                "no trust_score available; provide trust_score or metrics",
                code="INVALID_REQUEST",
            )

    proposal = UpdateProposal(
        agent_name=name,
        code_change_ratio=float(payload.get("code_change_ratio", 0.0)),
        breaking_changes=bool(payload.get("breaking_changes", False)),
        new_dependencies=bool(payload.get("new_dependencies", False)),
        policy_change=bool(payload.get("policy_change", False)),
        affects_governance=bool(payload.get("affects_governance", False)),
    )
    decision = SelfUpdateGovernor().classify(proposal, trust_score, has_history)
    result = decision.to_dict()
    result["trust_score"] = trust_score
    return result


def _checkpoint_summary(record: dict) -> Dict[str, Any]:
    state = record.get("state", {})
    registry = state.get("registry_snapshot", {})
    hebbian = state.get("config_snapshot", {}).get("hebbian", {})
    return {
        "checkpoint_id": record.get("checkpoint_id"),
        "timestamp": record.get("timestamp"),
        "type": record.get("type"),
        "metadata": record.get("metadata", {}),
        "retention": record.get("retention", {}),
        "verified": record.get("verified", False),
        "agent_count": len(registry.get("agents", [])),
        "violation_count": len(registry.get("violations", [])),
        "hebbian_tables": sorted((hebbian.get("tables") or {}).keys()),
    }


def _create_checkpoint(payload: Dict[str, Any]) -> Dict[str, Any]:
    checkpoint_type = str(payload.get("checkpoint_type", "manual"))
    metadata = payload.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise BridgeError("metadata must be an object", code="INVALID_REQUEST")
    retention_days = payload.get("retention_days", 60)
    try:
        retention_days = int(retention_days)
    except (TypeError, ValueError):
        raise BridgeError("retention_days must be an integer", code="INVALID_REQUEST")
    if retention_days < 1:
        raise BridgeError("retention_days must be >= 1", code="INVALID_REQUEST")
    try:
        record = _checkpoint_store(payload).create_checkpoint(
            _store(payload).export_snapshot(),
            checkpoint_type=checkpoint_type,
            metadata=metadata,
            retention_days=retention_days,
            config_snapshot={"hebbian": _hebbian_manager(payload).export_snapshot()},
        )
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST")
    return _checkpoint_summary(record)


def _list_checkpoints(payload: Dict[str, Any]) -> Dict[str, Any]:
    store = _checkpoint_store(payload)
    records = store.list_checkpoints()
    return {
        "checkpoints": [_checkpoint_summary(record) for record in records],
        "count": len(records),
    }


def _get_checkpoint(payload: Dict[str, Any]) -> Dict[str, Any]:
    checkpoint_id = _require_str(payload, "checkpoint_id")
    store = _checkpoint_store(payload)
    record = store.get_checkpoint(checkpoint_id)
    if record is None:
        raise BridgeError("checkpoint not found", code="NOT_FOUND")
    return {
        **_checkpoint_summary(record),
        "integrity_valid": store.verify_checkpoint(checkpoint_id),
    }


def _rollback_checkpoint(payload: Dict[str, Any]) -> Dict[str, Any]:
    checkpoint_id = _require_str(payload, "checkpoint_id")
    initiated_by = _require_str(payload, "initiated_by")
    if payload.get("confirmed") is not True:
        raise BridgeError(
            "confirmed=true is required for rollback",
            code="INVALID_REQUEST",
        )
    checkpoint_store = _checkpoint_store(payload)
    try:
        rollback = RollbackManager(checkpoint_store).rollback_to(
            checkpoint_id,
            initiated_by,
            reason=str(payload.get("reason", "manual_request")),
            details=str(payload.get("details", "")),
        )
        registry_store = _store(payload)
        registry_result = registry_store.restore_snapshot(rollback["restored_state"])
        hebbian_snapshot = rollback.get("restored_config", {}).get("hebbian")
        hebbian_result = None
        if hebbian_snapshot:
            hebbian_result = _hebbian_manager(payload).restore_snapshot(
                hebbian_snapshot
            )
        coordinator = _learning_governance(payload)
        for agent in registry_store.list_agent_records():
            coordinator.reconcile_agent(agent["name"])
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST")
    return {
        "rollback_id": rollback["rollback_id"],
        "checkpoint_id": checkpoint_id,
        "status": "restored",
        "registry": registry_result,
        "hebbian": hebbian_result,
    }


def _parse_atp(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_input = _require_str(payload, "message")
    message, metrics = ATPParser().parse_with_metrics(raw_input)
    return {"message": message.to_dict(), "metrics": metrics}


def _validate_atp(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_input = _require_str(payload, "message")
    strict = bool(payload.get("strict", False))
    message = ATPParser().parse(raw_input)
    validation = ATPValidator(strict=strict).validate(message)
    return {
        "message": message.to_dict(),
        "validation": _validation_to_dict(validation),
    }


def _send_atp(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_input = (
        payload.get("message") or payload.get("text") or payload.get("atpMessage")
    )
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise BridgeError("missing required field: message", code="INVALID_REQUEST")
    strict = bool(payload.get("strict", False))
    parsed = _parse_and_validate_atp(raw_input, strict=strict)
    message_id = str(uuid.uuid4())
    provenance_id = _record_atp_prompt_provenance(
        payload,
        raw_input,
        parsed["message"],
        source="api_bridge.atp.send",
    )
    now = _now_iso()
    db_path = _ensure_atp_store(payload)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO atp_messages (
                message_id, raw_message, parsed_json, validation_json,
                status, provenance_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                raw_input,
                json.dumps(parsed["message"]),
                json.dumps(parsed["validation"]),
                "queued",
                provenance_id,
                now,
                now,
            ),
        )
        conn.commit()
    return {
        "message_id": message_id,
        "status": "queued",
        "message": parsed["message"],
        "validation": parsed["validation"],
        "provenance_id": provenance_id,
        "queued_at": now,
    }


def _infer_route_capability(message: Dict[str, Any], payload: Dict[str, Any]) -> str:
    explicit = payload.get("required_capability") or payload.get("capability")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    target = str(message.get("target_zone") or "").lower()
    action = str(message.get("action_type") or "").lower()
    mode = str(message.get("mode") or "").lower()
    if "memory" in target:
        return "memory_inspection"
    if "summarize" in action or mode == "synthesize":
        return "text_summarization"
    if "review" in mode or "reflect" in action:
        return "reasoning"
    if "plan" in target or "scaffold" in action:
        return "planning"
    return "llm_chat"


def _bridge_routing_registry(payload: Dict[str, Any]) -> AgentRegistry:
    """Hydrate the registry facade from its authoritative persisted records."""
    registry = AgentRegistry(db_path=_resolve_db_path(payload))
    for record in registry.store.list_agent_records():
        name = record.get("name")
        if not isinstance(name, str):
            continue
        registry.agents[name] = cast(
            Any,
            SimpleNamespace(
                name=name,
                capabilities=list(record.get("capabilities") or []),
            ),
        )
    return registry


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _route_atp(payload: Dict[str, Any]) -> Dict[str, Any]:
    message_id = payload.get("message_id") or payload.get("id")
    db_path = _ensure_atp_store(payload)
    raw_input = (
        payload.get("message") or payload.get("text") or payload.get("atpMessage")
    )
    stored: Dict[str, Any] | None = None
    if message_id:
        if not isinstance(message_id, str):
            raise BridgeError("message_id must be a string", code="INVALID_REQUEST")
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM atp_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        if row is None:
            raise BridgeError(f"ATP message not found: {message_id}", code="NOT_FOUND")
        stored = _atp_row_to_dict(row)
        raw_input = stored["raw_message"]
    if not isinstance(raw_input, str) or not raw_input.strip():
        raise BridgeError("missing required field: message", code="INVALID_REQUEST")

    parsed = _parse_and_validate_atp(raw_input, strict=bool(payload.get("strict")))
    explicit = payload.get("required_capability") or payload.get("capability")
    task_context: Dict[str, Any] = {
        "content": raw_input,
        "context": raw_input,
        "_capability_explicit": bool(explicit),
    }
    if explicit:
        task_context["required_capability"] = str(explicit)
    task_context = resolve_task_context(
        task_context,
        strict=bool(payload.get("strict")),
        default_capability="llm_chat",
    )
    if not task_context.get("required_capability"):
        task_context["required_capability"] = _infer_route_capability(
            parsed["message"], payload
        )
    task_context.setdefault("routing_scope", task_context["required_capability"])

    provenance_id = stored.get("provenance_id") if stored else None
    if not provenance_id:
        provenance_id = _record_atp_prompt_provenance(
            payload,
            raw_input,
            parsed["message"],
            source="api_bridge.atp.route",
        )

    fallback = os.getenv("ARTEMIS_ROUTING_FALLBACK_CAPABILITY", "llm_chat").strip()
    router = HebbianRouter(
        _bridge_routing_registry(payload),
        _hebbian_manager(payload),
        alpha=_env_float("ARTEMIS_HEBBIAN_ROUTING_ALPHA", 0.3),
        beta=_env_float("ARTEMIS_HEBBIAN_ROUTING_BETA", 0.0),
        trust_floor=_env_float("ARTEMIS_ROUTING_TRUST_FLOOR", 0.0),
        trust_interface=_trust_interface(payload),
        fallback_capability=fallback or None,
    )
    try:
        decision = router.route(task_context)
    except ValueError as exc:
        raise BridgeError(str(exc), code="NOT_FOUND") from exc
    route = decision.to_dict() | {
        "required_capability": task_context["required_capability"],
        "fallback_used": decision.fallback_from is not None,
        "provenance_id": provenance_id,
    }
    _record_atp_child_provenance(
        payload,
        provenance_id,
        "atp_routing_decision",
        {"route": route, "validation": parsed["validation"]},
    )
    if stored:
        now = _now_iso()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE atp_messages
                SET status = ?, route_json = ?, provenance_id = ?, updated_at = ?
                WHERE message_id = ?
                """,
                ("routed", json.dumps(route), provenance_id, now, message_id),
            )
            conn.commit()
    return {
        "message": parsed["message"],
        "validation": parsed["validation"],
        "route": route,
        "provenance_id": provenance_id,
    }


def _atp_modes(_: Dict[str, Any]) -> Dict[str, Any]:
    return {"modes": [mode.value for mode in ATPMode if mode != ATPMode.UNKNOWN]}


def _atp_priorities(_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "priorities": [
            priority.value
            for priority in ATPPriority
            if priority != ATPPriority.UNKNOWN
        ]
    }


def _atp_action_types(_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "action_types": [
            action.value for action in ATPActionType if action != ATPActionType.UNKNOWN
        ]
    }


def _atp_template(_: Dict[str, Any]) -> Dict[str, Any]:
    template = "\n".join(
        [
            "#Mode: Build",
            "#Context: ",
            "#Priority: Normal",
            "#ActionType: Execute",
            "#TargetZone: ",
            "#SpecialNotes: ",
            "",
            "",
        ]
    )
    return {"template": template}


def _format_atp(payload: Dict[str, Any]) -> Dict[str, Any]:
    source = payload.get("message")
    if isinstance(source, dict):
        data = source
    else:
        data = payload
    lines = [
        f"#Mode: {data.get('mode', 'Build')}",
        f"#Context: {data.get('context', '')}",
        f"#Priority: {data.get('priority', 'Normal')}",
        f"#ActionType: {data.get('action_type') or data.get('actionType') or 'Execute'}",
        f"#TargetZone: {data.get('target_zone') or data.get('targetZone') or ''}",
        f"#SpecialNotes: {data.get('special_notes') or data.get('specialNotes') or ''}",
        "",
        str(data.get("content", "")),
    ]
    formatted = "\n".join(lines)
    return {"formatted": formatted, "parsed": ATPParser().parse(formatted).to_dict()}


def _get_atp_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    message_id = _require_str(payload, "message_id")
    db_path = _ensure_atp_store(payload)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM atp_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
    if row is None:
        raise BridgeError(f"ATP message not found: {message_id}", code="NOT_FOUND")
    return _atp_row_to_dict(row)


def _get_atp_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    record = _get_atp_message(payload)
    return {
        "message_id": record["message_id"],
        "status": record["status"],
        "response": record["response"],
        "route": record["route"],
    }


def _atp_queue(payload: Dict[str, Any]) -> Dict[str, Any]:
    limit = _optional_limit(payload, default=50, maximum=200)
    db_path = _ensure_atp_store(payload)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM atp_messages GROUP BY status"
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT * FROM atp_messages ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "messages": [_atp_row_to_dict(row) for row in rows],
    }


def _memory_read(payload: Dict[str, Any]) -> Dict[str, Any]:
    path = _require_note_path(payload)
    try:
        manager = _memory_manager(payload)
        content = manager.read_note(path)
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST") from exc
    if content is None:
        raise BridgeError(f"note not found: {path}", code="NOT_FOUND")
    return {
        "status": "success",
        "source": "exact",
        "path": path,
        "content": content,
        "score": 1.0,
    }


def _memory_write(payload: Dict[str, Any]) -> Dict[str, Any]:
    path = _require_note_path(payload)
    content = _require_str(payload, "content", allow_empty=True)
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise BridgeError("metadata must be an object", code="INVALID_REQUEST")
    embed = payload.get("embed", True)
    if not isinstance(embed, bool):
        raise BridgeError("embed must be a boolean", code="INVALID_REQUEST")

    try:
        _, bus = _memory_dependencies(payload)
        return bus.write_note_with_embedding(
            path,
            content,
            metadata=metadata,
            embed=embed,
        )
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST") from exc


def _memory_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    path = _optional_note_path(payload)
    suffix = payload.get("suffix", ".md")
    if not isinstance(suffix, str):
        raise BridgeError("suffix must be a string", code="INVALID_REQUEST")
    try:
        manager = _memory_manager(payload)
        files = manager.list_notes_in_folder(path, suffix=suffix)
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST") from exc
    return {"path": path, "files": files, "count": len(files)}


def _memory_delete(payload: Dict[str, Any]) -> Dict[str, Any]:
    path = _require_note_path(payload)
    try:
        manager, bus = _memory_dependencies(payload)
        deleted = manager.delete_note(path)
        if deleted:
            bus.vector_store.delete(bus._normalize_doc_id(path))
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST") from exc
    if not deleted:
        raise BridgeError(f"note not found: {path}", code="NOT_FOUND")
    return {"status": "success", "path": path, "deleted": True}


def _memory_stats(payload: Dict[str, Any]) -> Dict[str, Any]:
    path = _optional_note_path(payload)
    suffix = payload.get("suffix", ".md")
    if not isinstance(suffix, str):
        raise BridgeError("suffix must be a string", code="INVALID_REQUEST")
    try:
        manager, bus = _memory_dependencies(payload)
        root = manager._get_full_path(path)
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST") from exc

    files: list[Path] = []
    total_bytes = 0
    if root.exists():
        if root.is_file():
            files = [root] if root.suffix == suffix else []
        else:
            files = [item for item in root.rglob(f"*{suffix}") if item.is_file()]
        total_bytes = sum(item.stat().st_size for item in files)
    return {
        "status": "success",
        "path": path,
        "note_count": len(files),
        "total_bytes": total_bytes,
        "vector_count": bus.vector_store.count(),
    }


def _memory_search(payload: Dict[str, Any]) -> Dict[str, Any]:
    query = _require_str(payload, "query")
    limit = _optional_limit(payload, default=10, maximum=100)
    path = _optional_note_path(payload, default="")
    tags = payload.get("tags")
    if tags is not None and (
        not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags)
    ):
        raise BridgeError("tags must be a list of strings", code="INVALID_REQUEST")
    try:
        _, bus = _memory_dependencies(payload)
        results = bus.read(query, relative_path=path or None, max_results=limit)
    except ValueError as exc:
        raise BridgeError(str(exc), code="INVALID_REQUEST") from exc
    if tags:
        normalized_tags = [tag if tag.startswith("#") else f"#{tag}" for tag in tags]
        results = [
            result
            for result in results
            if any(tag in result.get("content", "") for tag in normalized_tags)
        ]
    return {"query": query, "results": results, "count": len(results)}


def _trust_get_score(payload: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = _require_str(payload, "entity_id")
    entity_type = payload.get("entity_type", "agent")
    if not isinstance(entity_type, str):
        raise BridgeError("entity_type must be a string", code="INVALID_REQUEST")
    score = _trust_interface(payload).get_trust_score(entity_id, entity_type)
    return score.to_dict()


def _trust_set_score(payload: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = _require_str(payload, "entity_id")
    entity_type = payload.get("entity_type", "agent")
    if not isinstance(entity_type, str):
        raise BridgeError("entity_type must be a string", code="INVALID_REQUEST")
    score_value = _optional_float(payload, "score", None, 0.0, 1.0)
    if score_value is None:
        raise BridgeError("missing required field: score", code="INVALID_REQUEST")
    updated = _learning_governance(payload).set_trust_score(
        entity_id,
        score_value,
        entity_type=entity_type,
    )
    return updated.to_dict()


def _trust_record_success(payload: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = _require_str(payload, "entity_id")
    entity_type = payload.get("entity_type", "agent")
    amount = _optional_float(payload, "amount", 0.02, 0.0, 1.0)
    assert amount is not None
    updated = _learning_governance(payload).record_trust_adjustment(
        entity_id,
        success=True,
        amount=amount,
        entity_type=str(entity_type),
    )
    return updated.to_dict() | {
        "recorded": "success",
        "score": updated.score,
    }


def _trust_record_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = _require_str(payload, "entity_id")
    entity_type = payload.get("entity_type", "agent")
    amount = _optional_float(payload, "amount", 0.05, 0.0, 1.0)
    assert amount is not None
    updated = _learning_governance(payload).record_trust_adjustment(
        entity_id,
        success=False,
        amount=amount,
        entity_type=str(entity_type),
    )
    return updated.to_dict() | {
        "recorded": "failure",
        "score": updated.score,
    }


def _trust_permissions(payload: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = _require_str(payload, "entity_id")
    entity_type = str(payload.get("entity_type", "agent"))
    interface = _trust_interface(payload)
    trust_score = interface.get_trust_score(entity_id, entity_type)
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "trust": trust_score.to_dict(),
        "permissions": interface.OPERATION_PERMISSIONS.get(trust_score.level, []),
    }


def _trust_can_perform(payload: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = _require_str(payload, "entity_id")
    operation = _require_str(payload, "operation")
    entity_type = str(payload.get("entity_type", "agent"))
    allowed = _trust_interface(payload).can_perform_operation(
        entity_id, operation, entity_type
    )
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "operation": operation,
        "allowed": allowed,
    }


def _trust_levels(_: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "levels": [
            {
                "level": level.value,
                "threshold": TRUST_THRESHOLDS[level],
            }
            for level in TrustLevel
        ]
    }


def _trust_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _trust_interface(payload).get_trust_report()


def _trust_apply_decay(payload: Dict[str, Any]) -> Dict[str, Any]:
    decay_rate = _optional_float(payload, "decay_rate", None, 0.0, 1.0)
    updated = _learning_governance(payload).apply_trust_decay(decay_rate)
    return {"updated": updated, "count": len(updated)}


def _hebbian_weights(payload: Dict[str, Any]) -> Dict[str, Any]:
    limit = _optional_limit(payload, default=50, maximum=500)
    min_weight = _optional_float(payload, "min_weight", 0.0) or 0.0
    manager = _hebbian_manager(payload)
    connections = manager.get_all_connections(min_weight=min_weight)[:limit]
    return {"summary": manager.get_network_summary(), "connections": connections}


def _hebbian_sentinel_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    limit = _optional_limit(payload, default=100, maximum=500)
    agent_name = payload.get("agent_name")
    task_type = payload.get("task_type")
    if agent_name is not None and not isinstance(agent_name, str):
        raise BridgeError("agent_name must be a string", code="INVALID_REQUEST")
    if task_type is not None and not isinstance(task_type, str):
        raise BridgeError("task_type must be a string", code="INVALID_REQUEST")
    signals = _hebbian_manager(payload).list_sentinel_status(
        agent_name=agent_name,
        task_type=task_type,
        limit=limit,
    )
    return {"signals": signals, "total": len(signals)}


def _hebbian_sentinel_alerts(payload: Dict[str, Any]) -> Dict[str, Any]:
    limit = _optional_limit(payload, default=100, maximum=500)
    open_only = payload.get("open_only", False)
    if not isinstance(open_only, bool):
        raise BridgeError("open_only must be a boolean", code="INVALID_REQUEST")
    alerts = _hebbian_manager(payload).list_sentinel_alerts(
        open_only=open_only,
        limit=limit,
    )
    return {"alerts": alerts, "total": len(alerts)}


def _hebbian_update(payload: Dict[str, Any]) -> Dict[str, Any]:
    origin = payload.get("origin") or payload.get("agent1")
    target = payload.get("target") or payload.get("agent2")
    if not isinstance(origin, str) or not origin.strip():
        raise BridgeError("missing required field: origin", code="INVALID_REQUEST")
    if not isinstance(target, str) or not target.strip():
        raise BridgeError("missing required field: target", code="INVALID_REQUEST")
    delta = _optional_float(payload, "delta", None)
    if delta is None:
        raise BridgeError("missing required field: delta", code="INVALID_REQUEST")
    coordinator = _learning_governance(
        payload,
        include_hebbian=True,
        include_trust=False,
    )
    coordinator.create_checkpoint(f"before_hebbian_update:{origin}:{target}")
    manager = coordinator.hebbian_manager
    assert manager is not None
    current = manager.get_weight(origin, target)
    new_weight = max(0.0, current + delta)
    now = datetime.now().isoformat()
    with sqlite3.connect(manager.db_path) as conn:
        conn.execute(
            """
            INSERT INTO node_connections (
                origin_node, target_node, weight, activation_count, success_count,
                failure_count, last_updated, created_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(origin_node, target_node)
            DO UPDATE SET
                weight = ?,
                activation_count = activation_count + 1,
                success_count = success_count + ?,
                failure_count = failure_count + ?,
                last_updated = ?
            """,
            (
                origin,
                target,
                new_weight,
                1 if delta > 0 else 0,
                1 if delta < 0 else 0,
                now,
                now,
                new_weight,
                1 if delta > 0 else 0,
                1 if delta < 0 else 0,
                now,
            ),
        )
        conn.commit()
    result = manager.get_connection_stats(origin, target) or {}
    task_type = (
        target.removeprefix("task_type:") if target.startswith("task_type:") else None
    )
    coordinator.mirror_hebbian_summary(
        origin,
        task_type=task_type,
    )
    return result


COMMANDS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "llm.chat": _llm_chat,
    "llm.complete": _llm_complete,
    "llm.config": _llm_config,
    "atp.parse": _parse_atp,
    "atp.validate": _validate_atp,
    "atp.send": _send_atp,
    "atp.route": _route_atp,
    "atp.modes": _atp_modes,
    "atp.priorities": _atp_priorities,
    "atp.action_types": _atp_action_types,
    "atp.template": _atp_template,
    "atp.format": _format_atp,
    "atp.get_message": _get_atp_message,
    "atp.get_response": _get_atp_response,
    "atp.queue": _atp_queue,
    "memory.read": _memory_read,
    "memory.write": _memory_write,
    "memory.list": _memory_list,
    "memory.search": _memory_search,
    "memory.delete": _memory_delete,
    "memory.stats": _memory_stats,
    "registry.list_agents": _list_agents,
    "registry.get_agent": _get_agent,
    "registry.register_agent": _register_agent,
    "registry.update_agent": _update_agent,
    "registry.delete_agent": _delete_agent,
    "registry.set_agent_status": _set_agent_status,
    "registry.get_violations": _get_violations,
    "registry.clear_violations": _clear_violations,
    "registry.set_trust_tier": _set_trust_tier,
    "registry.record_violation": _record_violation,
    "governance.compute_trust": _compute_trust,
    "governance.evaluate_update": _evaluate_update,
    "governance.checkpoints.create": _create_checkpoint,
    "governance.checkpoints.list": _list_checkpoints,
    "governance.checkpoints.get": _get_checkpoint,
    "governance.checkpoints.rollback": _rollback_checkpoint,
    "trust.get_score": _trust_get_score,
    "trust.set_score": _trust_set_score,
    "trust.record_success": _trust_record_success,
    "trust.record_failure": _trust_record_failure,
    "trust.permissions": _trust_permissions,
    "trust.can_perform": _trust_can_perform,
    "trust.levels": _trust_levels,
    "trust.report": _trust_report,
    "trust.apply_decay": _trust_apply_decay,
    "hebbian.weights": _hebbian_weights,
    "hebbian.sentinel_status": _hebbian_sentinel_status,
    "hebbian.sentinel_alerts": _hebbian_sentinel_alerts,
    "hebbian.update": _hebbian_update,
}


def dispatch(command: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Run a bridge command and return its JSON-able result dict.

    Raises :class:`BridgeError` for unknown commands or handler failures.

    Args:
        command (str): Command text to process.
        payload (Dict[str, Any] | None): Request payload passed through the bridge or API
            layer.

    Returns:
        Dict[str, Any]: Dictionary containing the resulting data.
    """
    handler = COMMANDS.get(command)
    if handler is None:
        raise BridgeError(f"unknown command: {command}", code="UNKNOWN_COMMAND")
    return handler(payload or {})


def main(argv=None) -> int:
    """CLI entry point: read one JSON request from stdin, write one to stdout.

    Args:
        argv: Argv value used by this operation.

    Returns:
        int: Integer result produced by the operation.
    """
    raw = sys.stdin.read()
    try:
        request = json.loads(raw) if raw.strip() else {}
        command = request.get("command")
        if not command:
            raise BridgeError("missing 'command'", code="INVALID_REQUEST")
        data = dispatch(command, request.get("payload") or {})
        sys.stdout.write(json.dumps({"ok": True, "data": data}))
        return 0
    except BridgeError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc), "code": exc.code}))
        return 1
    except json.JSONDecodeError as exc:
        sys.stdout.write(
            json.dumps(
                {"ok": False, "error": f"invalid JSON: {exc}", "code": "INVALID_JSON"}
            )
        )
        return 1
    except Exception as exc:  # pragma: no cover - defensive catch-all
        sys.stdout.write(
            json.dumps({"ok": False, "error": str(exc), "code": "INTERNAL_ERROR"})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
