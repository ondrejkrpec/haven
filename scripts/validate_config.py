#!/usr/bin/env python3
"""Validate Haven YAML layers without resolving credentials or changing a service."""

from __future__ import annotations

import argparse
from copy import deepcopy
import ipaddress
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 65536
MAX_DEPTH = 32
MAX_EVENTS = 10000


class ConfigError(ValueError):
    """A diagnostic safe to print: no input values or source excerpts."""


class StrictLoader(yaml.SafeLoader):
    """Safe YAML with unique string keys and explicit lowercase booleans."""

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise ConfigError("expected a mapping")
        result = {}
        for key_node, value_node in node.value:
            if key_node.tag != "tag:yaml.org,2002:str":
                raise ConfigError("mapping keys must be strings; YAML merge keys are unsupported")
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ConfigError(f"duplicate mapping key at line {key_node.start_mark.line + 1}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


# Do not mutate PyYAML's global SafeLoader or implicitly turn dates into objects.
StrictLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in entries
          if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}]
    for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(true|false)$"), list("tf")
)


def load_yaml(path: Path, label: str) -> dict:
    """Bound parsing, reject YAML indirection and redact parser exceptions."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ConfigError(f"document exceeds {MAX_BYTES} bytes")
        text = raw.decode("utf-8")
        depth = 0
        for count, event in enumerate(yaml.parse(text, Loader=StrictLoader), 1):
            if count > MAX_EVENTS:
                raise ConfigError("too many YAML nodes")
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ConfigError("YAML anchors and aliases are unsupported")
            if getattr(event, "tag", None) is not None:
                raise ConfigError("explicit YAML tags are unsupported")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > MAX_DEPTH:
                    raise ConfigError(f"YAML nesting exceeds {MAX_DEPTH} levels")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        document = yaml.load(text, Loader=StrictLoader)
        if not isinstance(document, dict):
            raise ConfigError("document must be a non-empty mapping")
        return document
    except ConfigError as error:
        raise ConfigError(f"{label}: {error}") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"{label}: invalid or unsupported YAML{where}") from None
    except UnicodeError:
        raise ConfigError(f"{label}: document must be UTF-8") from None
    except (ValueError, OverflowError):
        raise ConfigError(f"{label}: invalid or unsupported YAML scalar") from None
    except OSError:
        raise ConfigError(f"{label}: cannot read configuration file") from None


FORMATS = FormatChecker(formats=[])


@FORMATS.checks("iana-timezone")
def valid_timezone(value):
    if not isinstance(value, str):
        return True  # The schema's type check reports this.
    try:
        ZoneInfo(value)
        return True
    except (ValueError, ZoneInfoNotFoundError):
        return False


@FORMATS.checks("host-address")
def valid_host(value):
    if not isinstance(value, str):
        return True
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        # Reject ambiguous dotted numeric input rather than treating it as DNS.
        if re.fullmatch(r"[0-9.]+", value):
            return False
        labels = value.removesuffix(".").split(".")
        return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                   for label in labels)


@FORMATS.checks("secret-reference")
def valid_secret_reference(value):
    if not isinstance(value, str):
        return True
    if value.startswith("env:"):
        return bool(re.fullmatch(r"env:[A-Z_][A-Z0-9_]*", value))
    if value.startswith("file:/"):
        path = value[5:]
        return (bool(re.fullmatch(r"/[A-Za-z0-9_./-]+", path))
                and all(part not in {"", ".", ".."} for part in path[1:].split("/")))
    return False


def partial_schema(schema):
    """Layers retain field constraints; completeness and conditions apply after merge."""
    if isinstance(schema, dict):
        return {key: partial_schema(value) for key, value in schema.items()
                if key not in {"required", "if", "then", "else"}}
    if isinstance(schema, list):
        return [partial_schema(value) for value in schema]
    return schema


def merge(base: dict, override: dict) -> dict:
    """Mappings recurse, all other values (including lists) replace atomically."""
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def diagnostic(error, schema: dict) -> str:
    # Only schema-defined field names appear in paths, never arbitrary user keys.
    path = "$"
    cursor = schema
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
            cursor = cursor.get("items", {})
        elif part in cursor.get("properties", {}):
            path += f".{part}"
            cursor = cursor["properties"][part]
        else:
            path += ".<unknown>"
            cursor = {}
    kind = error.validator
    if kind == "required":
        missing = [key for key in error.schema["required"] if key not in error.instance]
        message = "missing required field(s): " + ", ".join(missing)
    elif kind == "additionalProperties":
        message = "unknown field(s); supported: " + ", ".join(error.schema.get("properties", {}))
    elif kind == "type":
        message = f"must be {error.validator_value}"
    elif kind == "format":
        message = {
            "iana-timezone": "must be a known IANA time zone (system tzdata required)",
            "host-address": "must be a hostname or IP address without a URL, user or port",
            "secret-reference": "must be env:VARIABLE_NAME or file:/absolute/path",
        }[error.validator_value]
    elif kind == "const":
        message = "unsupported schema_version; expected 1" if path == "$.schema_version" else "unsupported value"
    elif kind == "not":
        message = "root cannot be the service user"
    elif kind == "pattern":
        message = "has an invalid syntax; see the configuration reference"
    elif kind == "uniqueItems":
        message = "must not contain duplicate items"
    elif kind in {"minItems", "maxItems", "minLength", "maxLength"}:
        bound = "at least" if kind.startswith("min") else "at most"
        unit = "items" if kind.endswith("Items") else "characters"
        message = f"must contain {bound} {error.validator_value} {unit}"
    else:
        message = "does not satisfy the schema"
    return f"{path}: {message}"


def validate(document: dict, schema: dict, label: str) -> None:
    errors = list(Draft202012Validator(schema, format_checker=FORMATS).iter_errors(document))
    if errors:
        # Do not print jsonschema's message, instance or exception representation.
        messages = sorted({diagnostic(error, schema) for error in errors})
        raise ConfigError(label + ":\n  " + "\n  ".join(messages[:20])
                          + ("\n  further errors omitted" if len(messages) > 20 else ""))


def load_config(config: Path, overrides=()) -> dict:
    schema = json.loads((ROOT / "schemas/instance-v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    layer_schema = partial_schema(schema)
    layer_schema["required"] = ["schema_version"]
    effective = {}
    layers = [(ROOT / "config/defaults.yaml", "defaults"), (Path(config), "config")]
    layers += [(Path(path), f"override {i}") for i, path in enumerate(overrides, 1)]
    for path, label in layers:
        layer = load_yaml(path, label)
        validate(layer, layer_schema, label)
        effective = merge(effective, layer)
    validate(effective, schema, "effective configuration")
    return effective


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="base YAML file (public defaults are applied first)")
    parser.add_argument("--override", type=Path, action="append", default=[],
                        help="explicit override YAML file; repeat in application order")
    args = parser.parse_args(argv)
    try:
        load_config(args.config, args.override)
    except ConfigError as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return 1
    print("Configuration valid (schema_version: 1).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
