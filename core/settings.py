"""Configuration loading, environment substitution, decryption and validation."""

from __future__ import annotations

import copy
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from core.crypto import CryptoError, SecretBox, is_encrypted

logger = logging.getLogger(__name__)

ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")

SUPPORTED_DB_TYPES = {"mssql", "oracle", "sqlite"}
SUPPORTED_FIELD_TYPES = {"string", "int", "decimal", "bool", "date", "datetime", "constant"}


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed or inconsistent."""


def _substitute_env(value: str) -> str:
    def replace(match: re.Match) -> str:
        name = match.group("name")
        default = match.group("default")
        resolved = os.environ.get(name)
        if resolved is None or resolved == "":
            return default if default is not None else ""
        return resolved

    return ENV_PATTERN.sub(replace, value)


def _resolve_env(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _resolve_env(val) for key, val in node.items()}
    if isinstance(node, list):
        return [_resolve_env(item) for item in node]
    if isinstance(node, str):
        return _substitute_env(node)
    return node


def _decrypt_tree(node: Any, box: Optional[SecretBox], path: str = "") -> Any:
    if isinstance(node, dict):
        return {key: _decrypt_tree(val, box, f"{path}.{key}" if path else key) for key, val in node.items()}
    if isinstance(node, list):
        return [_decrypt_tree(item, box, f"{path}[{idx}]") for idx, item in enumerate(node)]
    if is_encrypted(node):
        if box is None:
            raise ConfigError(f"{path} is encrypted but no master key is available")
        try:
            return box.unwrap(node)
        except CryptoError as exc:
            raise ConfigError(f"Cannot decrypt {path}: {exc}") from exc
    return node


def _contains_encrypted(node: Any) -> bool:
    if isinstance(node, dict):
        return any(_contains_encrypted(val) for val in node.values())
    if isinstance(node, list):
        return any(_contains_encrypted(item) for item in node)
    return is_encrypted(node)


def _require(mapping: dict, key: str, where: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"Missing required setting {where}.{key}")
    return mapping[key]


@dataclass
class Settings:
    raw: dict
    path: Path
    secret_box: Optional[SecretBox] = field(default=None, repr=False)

    @property
    def service(self) -> dict:
        return self.raw.get("service", {})

    @property
    def logging(self) -> dict:
        return self.raw.get("logging", {})

    @property
    def security(self) -> dict:
        return self.raw.get("security", {})

    @property
    def cache(self) -> dict:
        return self.raw.get("cache", {})

    @property
    def databases(self) -> dict:
        return self.raw.get("databases", {})

    @property
    def sap(self) -> dict:
        return self.raw.get("sap", {})

    @property
    def jobs(self) -> list[dict]:
        return [job for job in self.raw.get("jobs", []) if job.get("enabled", True)]

    def redacted(self) -> dict:
        """Copy of the configuration safe for logging (secrets masked)."""
        sensitive = ("password", "secret", "key", "token")

        def mask(node: Any, key: str = "") -> Any:
            if isinstance(node, dict):
                return {k: mask(v, k) for k, v in node.items()}
            if isinstance(node, list):
                return [mask(item, key) for item in node]
            if isinstance(node, str) and node and any(s in key.lower() for s in sensitive):
                if key.endswith(("_env", "_file", "key_file")):
                    return node
                return "***"
            return node

        return mask(copy.deepcopy(self.raw))


def validate(raw: dict) -> None:
    for section in ("service", "security", "cache", "databases", "sap", "jobs"):
        if section not in raw:
            raise ConfigError(f"Missing top-level section '{section}'")

    databases = raw["databases"]
    if not isinstance(databases, dict) or not databases:
        raise ConfigError("'databases' must be a non-empty mapping")
    for name, db in databases.items():
        db_type = _require(db, "type", f"databases.{name}")
        if db_type not in SUPPORTED_DB_TYPES:
            raise ConfigError(f"databases.{name}.type '{db_type}' not in {sorted(SUPPORTED_DB_TYPES)}")
        if db_type == "sqlite":
            _require(db, "path", f"databases.{name}")
        else:
            for key in ("host", "port", "username"):
                _require(db, key, f"databases.{name}")
        if db_type == "mssql":
            _require(db, "database", f"databases.{name}")
        if db_type == "oracle" and not (db.get("service_name") or db.get("dsn")):
            raise ConfigError(f"databases.{name} needs service_name or dsn")

    sap = raw["sap"]
    _require(sap, "base_url", "sap")
    auth_mode = sap.get("auth_mode", "oauth2")
    if auth_mode not in ("oauth2", "basic", "none"):
        raise ConfigError("sap.auth_mode must be oauth2, basic or none")
    if auth_mode == "oauth2":
        oauth = sap.get("oauth2", {})
        for key in ("token_url", "client_id"):
            _require(oauth, key, "sap.oauth2")
    if int(sap.get("batch_size", 100)) < 1:
        raise ConfigError("sap.batch_size must be >= 1")

    jobs = raw["jobs"]
    if not isinstance(jobs, list) or not jobs:
        raise ConfigError("'jobs' must be a non-empty list")
    seen: set[str] = set()
    for idx, job in enumerate(jobs):
        where = f"jobs[{idx}]"
        name = _require(job, "name", where)
        if name in seen:
            raise ConfigError(f"Duplicate job name '{name}'")
        seen.add(name)
        source = _require(job, "source", where)
        if source not in databases:
            raise ConfigError(f"{where}.source '{source}' is not defined in databases")
        query = _require(job, "query", where)
        if ":watermark" not in query:
            raise ConfigError(f"{where}.query must reference the :watermark bind parameter")
        _require(job, "watermark_column", where)
        sap_target = _require(job, "sap", where)
        _require(sap_target, "service_path", f"{where}.sap")
        _require(sap_target, "entity_set", f"{where}.sap")
        if int(job.get("chunk_size", 1000)) < 1:
            raise ConfigError(f"{where}.chunk_size must be >= 1")
        mapping = _require(job, "mapping", where)
        for target, rule in mapping.items():
            ftype = rule.get("type", "string")
            if ftype not in SUPPORTED_FIELD_TYPES:
                raise ConfigError(f"{where}.mapping.{target}.type '{ftype}' unsupported")
            if ftype == "constant" and "value" not in rule:
                raise ConfigError(f"{where}.mapping.{target} is constant but has no value")
            if ftype != "constant" and not rule.get("source"):
                raise ConfigError(f"{where}.mapping.{target} needs a source column")


def load_settings(path: str | os.PathLike, require_key: bool = False) -> Settings:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a mapping at the top level")

    resolved = _resolve_env(raw)
    security = resolved.get("security", {})
    env_name = security.get("master_key_env", "MITTELCONNECT_MASTER_KEY")
    key_file = security.get("master_key_file") or None

    key_required = (
        require_key
        or _contains_encrypted(resolved)
        or bool(security.get("encrypt_outbox", True))
    )
    box: Optional[SecretBox] = None
    try:
        box = SecretBox.from_settings(env_name, key_file)
    except CryptoError as exc:
        if key_required:
            raise ConfigError(str(exc)) from exc
        logger.warning("Running without a master key: %s", exc)

    decrypted = _decrypt_tree(resolved, box)
    validate(decrypted)
    return Settings(raw=decrypted, path=config_path.resolve(), secret_box=box)
