"""Deny-by-default deterministic dispatch with a private, bounded SQLite audit.

The hook is a convenience dispatcher. The independently armed native host policy
blocks all agent/auxiliary-model and tool execution even if the hook fails open.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config/capabilities.json"
REFUSAL = "Tento požadavek není povolený. Povolené operace zjistíš zprávou system.capabilities."
UNAVAILABLE = "Požadavek teď nelze bezpečně provést. Model ani nástroje nebyly spuštěné."
APP_ID = 0x48415643


class PolicyError(ValueError):
    """Fixed diagnostics only; no request text or private identifiers."""


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized(text):
    if not isinstance(text, str) or len(text) > 256 or any(ord(c) < 32 for c in text):
        return None
    return " ".join(text.strip().casefold().split()).removesuffix("?").strip()


def load_registry(path=POLICY):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {"policy_version", "retention_days", "max_audit_rows", "capabilities"}:
        raise PolicyError("registry_shape")
    if (not isinstance(data["policy_version"], str) or not data["policy_version"]
            or type(data["retention_days"]) is not int or not 1 <= data["retention_days"] <= 90
            or len(data["policy_version"]) > 64
            or type(data["max_audit_rows"]) is not int or not 1 <= data["max_audit_rows"] <= 50000
            or not isinstance(data["capabilities"], dict) or not 1 <= len(data["capabilities"]) <= 100):
        raise PolicyError("registry_limits")
    aliases = {}
    for name, entry in data["capabilities"].items():
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", name)
                or not isinstance(entry, dict)
                or set(entry) != {"enabled", "handler", "aliases", "max_llm_calls", "max_tokens",
                                  "max_cost_microusd", "max_requests_per_hour"}
                or type(entry["enabled"]) is not bool
                or entry["handler"] not in ("status", "capabilities", "unavailable")
                or any(type(entry[k]) is not int or entry[k] != 0
                       for k in ("max_llm_calls", "max_tokens", "max_cost_microusd"))
                or type(entry["max_requests_per_hour"]) is not int
                or not 0 <= entry["max_requests_per_hour"] <= 60
                or not isinstance(entry["aliases"], list) or not entry["aliases"]):
            raise PolicyError("registry_entry")
        if entry["enabled"] and (entry["handler"] == "unavailable" or entry["max_requests_per_hour"] == 0):
            raise PolicyError("unsupported_execution")
        for alias in entry["aliases"]:
            key = normalized(alias)
            if not key or key in aliases:
                raise PolicyError("registry_alias")
            aliases[key] = name
    return data, aliases


class Audit:
    def __init__(self, home, registry):
        self.directory = home / "state"
        self.path = self.directory / "capability-audit.sqlite3"
        self.registry = registry

    @contextmanager
    def transaction(self):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise PolicyError("audit_directory")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise PolicyError("audit_file")
        finally:
            os.close(descriptor)
        connection = sqlite3.connect(self.path, timeout=0.5)
        try:
            connection.execute("PRAGMA secure_delete=ON")
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            if application not in (0, APP_ID):
                raise PolicyError("audit_database")
            if application == 0:
                if connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                    raise PolicyError("audit_database")
                connection.execute(f"PRAGMA application_id={APP_ID}")
                connection.execute("PRAGMA user_version=1")
                connection.execute("""CREATE TABLE audit (
                    request_id TEXT PRIMARY KEY, created_at REAL NOT NULL, actor_hash TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL, request_bytes INTEGER NOT NULL,
                    capability TEXT, decision TEXT NOT NULL, reason TEXT NOT NULL,
                    result TEXT NOT NULL, reply TEXT NOT NULL, llm_calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_microusd INTEGER NOT NULL DEFAULT 0, policy_version TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL)""")
                connection.execute("CREATE INDEX capability_rate ON audit(actor_hash, capability, created_at)")
                connection.commit()
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise PolicyError("audit_schema")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append(self, db, request_id, actor, text, capability, decision, reason, result, reply):
        now = time.time()
        db.execute("DELETE FROM audit WHERE created_at < ?", (now - self.registry["retention_days"] * 86400,))
        if db.execute("SELECT count(*) FROM audit").fetchone()[0] >= self.registry["max_audit_rows"]:
            raise PolicyError("audit_capacity")
        raw = text.encode("utf-8")
        policy_hash = digest(json.dumps(self.registry, sort_keys=True, separators=(",", ":")))
        db.execute("""INSERT INTO audit(request_id,created_at,actor_hash,request_sha256,request_bytes,
                   capability,decision,reason,result,reply,policy_version,policy_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (request_id, now, actor, hashlib.sha256(raw).hexdigest(), len(raw), capability,
                    decision, reason, result, reply, self.registry["policy_version"], policy_hash))

    def guard_refusal(self, action):
        # This observer belongs to the launcher; it survives plugin failure/unloading.
        if action not in {"model", "auxiliary_model", "tool"}:
            raise PolicyError("guard_action")
        with self.transaction() as db:
            self.append(db, uuid.uuid4().hex, digest("host"), "", None, "deny",
                        "host_" + action + "_disabled", "blocked_before_execution", UNAVAILABLE)


def status(home):
    state = json.loads((home / "gateway_state.json").read_text())
    heartbeat = json.loads((home / "state/gateway.heartbeat").read_text())
    from datetime import datetime
    age = time.time() - datetime.fromisoformat(heartbeat["updated_at"]).timestamp()
    pid = os.getpid()
    if (state.get("pid") != pid or state.get("gateway_state") != "running"
            or heartbeat.get("pid") != pid or not 0 <= age < 120
            or state.get("platforms", {}).get("telegram", {}).get("state") != "connected"
            or state.get("platforms", {}).get("telegram", {}).get("writer_pid") != pid):
        raise PolicyError("status_not_ready")
    return "Nora běží, Telegram je připojený. Režim: pouze povolené operace, LLM volání a nástroje vypnuté."


class CapabilityService:
    def __init__(self, home, *, owner, policy=POLICY, status_handler=None):
        if not isinstance(owner, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", owner):
            raise PolicyError("owner_required")
        self.home, self.owner = home, owner
        self.registry, self.aliases = load_registry(policy)
        self.audit = Audit(home, self.registry)
        self.status_handler = status_handler or (lambda: status(home))

    def authorize(self, source):
        if (source.get("platform") != "telegram" or source.get("chat_type") != "dm"
                or source.get("user_id") != self.owner or source.get("chat_id") != self.owner
                or source.get("is_bot") is True):
            raise PolicyError("principal_denied")

    def execute(self, capability, source):
        # Re-check at the handler boundary, independently of hook classification.
        self.authorize(source)
        entry = self.registry["capabilities"].get(capability)
        if (not entry or entry["enabled"] is not True or entry["max_llm_calls"] != 0
                or entry["max_tokens"] != 0 or entry["max_cost_microusd"] != 0):
            raise PolicyError("capability_denied")
        if entry["handler"] == "status":
            return self.status_handler()
        if entry["handler"] == "capabilities":
            names = [name for name, item in self.registry["capabilities"].items() if item["enabled"]]
            return "Povolené operace: " + ", ".join(names) + ". Pošli přesný název; například system.status."
        raise PolicyError("handler_unavailable")

    def consume(self, session_key, platform, source, message_id, text, **kwargs):
        try:
            reply = self.dispatch(platform, source, message_id, text)
        except Exception:
            # Never leak input, SQLite/config errors or credentials, and never return None.
            reply = UNAVAILABLE
        return {"action": "handled", "reply": reply}

    def dispatch(self, platform, source, message_id, text):
        if not isinstance(text, str) or len(text) > 65536:
            raise PolicyError("request_size")
        if not isinstance(source, dict) or source.get("platform") != platform:
            raise PolicyError("source_required")
        self.authorize(source)
        identity = [platform, source.get("user_id"), source.get("chat_id"), message_id]
        if any(not isinstance(x, str) or not x or len(x) > 256 for x in identity):
            raise PolicyError("message_identity")
        request_id = digest(json.dumps(identity, separators=(",", ":")))
        actor = digest(json.dumps(identity[:3], separators=(",", ":")))
        capability = self.aliases.get(normalized(text))
        with self.audit.transaction() as db:
            old = db.execute("SELECT request_sha256, reply FROM audit WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old[0] != digest(text):
                    raise PolicyError("message_conflict")
                return old[1]
            decision, reason, result, reply = "deny", "unknown_operation", "not_executed", REFUSAL
            entry = self.registry["capabilities"].get(capability)
            if entry is not None:
                reason = "capability_disabled"
                if entry["enabled"]:
                    used = db.execute("""SELECT count(*) FROM audit WHERE actor_hash=? AND capability=?
                                      AND decision='allow' AND created_at>=?""",
                                      (actor, capability, time.time() - 3600)).fetchone()[0]
                    reason = "operation_rate_limit"
                    if used < entry["max_requests_per_hour"]:
                        try:
                            reply = self.execute(capability, source)
                            decision, reason, result = "allow", "explicit_capability", "succeeded"
                        except PolicyError as error:
                            reason, reply = str(error), UNAVAILABLE
                        except Exception:
                            reason, reply, result = "handler_failed", UNAVAILABLE, "failed"
            self.audit.append(db, request_id, actor, text, capability, decision, reason, result, reply)
        return reply

    def block_tool(self, **kwargs):
        self.audit.guard_refusal("tool")
        return {"block": True, "message": UNAVAILABLE}
