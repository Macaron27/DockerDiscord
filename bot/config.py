"""Environment configuration, validated once at startup (fail fast, never at 3am)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
# Docker's own container-name charset; also what the socket-proxy POST allowlist accepts.
CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
NUT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WolHost:
    mac: str
    ip: str
    port: int  # TCP port probed to decide "online" (a refused connection also counts)


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    guild_id: int | None
    allowed_user_ids: frozenset[int]
    alert_channel_id: int
    docker_host: str
    webhook_secret: str
    webhook_host: str
    webhook_port: int
    wol_hosts: Mapping[str, WolHost]
    wol_broadcast: str
    lockdown_containers: tuple[str, ...]
    nut_host: str | None
    nut_port: int
    nut_ups: str
    alert_cooldown_s: int
    log_level: str


def _csv(env: Mapping[str, str], key: str) -> list[str]:
    return [x.strip() for x in env.get(key, "").split(",") if x.strip()]


def _int(env: Mapping[str, str], key: str, default: int | None = None) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"{key} is required")
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def _wol_hosts(raw: str) -> dict[str, WolHost]:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"WOL_HOSTS is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError("WOL_HOSTS must be a JSON object")
    hosts: dict[str, WolHost] = {}
    for name, spec in data.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"WOL_HOSTS[{name!r}] must be an object")
        mac, ip = str(spec.get("mac", "")), str(spec.get("ip", ""))
        if not MAC_RE.match(mac):
            raise ConfigError(f"WOL_HOSTS[{name!r}].mac is not a MAC address: {mac!r}")
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            raise ConfigError(f"WOL_HOSTS[{name!r}].ip is not an IP address: {ip!r}") from None
        port = spec.get("port", 22)
        if not isinstance(port, int) or not 0 < port < 65536:
            raise ConfigError(f"WOL_HOSTS[{name!r}].port must be 1-65535")
        hosts[name] = WolHost(mac=mac, ip=ip, port=port)
    return hosts


def load(env: Mapping[str, str] = os.environ) -> Config:
    try:
        allowed = frozenset(int(x) for x in _csv(env, "ALLOWED_USER_IDS"))
    except ValueError:
        raise ConfigError("ALLOWED_USER_IDS must be comma-separated Discord user IDs") from None
    if not allowed:
        raise ConfigError("ALLOWED_USER_IDS is required (nobody could use the bot)")

    secret = _required(env, "WEBHOOK_SECRET")
    if len(secret) < 24:
        raise ConfigError("WEBHOOK_SECRET must be at least 24 characters")

    lockdown = tuple(_csv(env, "LOCKDOWN_CONTAINERS"))
    for name in lockdown:
        if not CONTAINER_NAME_RE.match(name):
            raise ConfigError(f"LOCKDOWN_CONTAINERS has an invalid container name: {name!r}")

    nut_ups = env.get("NUT_UPS", "ups").strip()
    if not NUT_NAME_RE.match(nut_ups):  # it is sent raw over the NUT text protocol
        raise ConfigError(f"NUT_UPS has invalid characters: {nut_ups!r}")

    log_level = env.get("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"LOG_LEVEL must be DEBUG/INFO/WARNING/ERROR/CRITICAL, got {log_level!r}")

    guild = env.get("GUILD_ID", "").strip()
    return Config(
        discord_token=_required(env, "DISCORD_TOKEN"),
        guild_id=_int(env, "GUILD_ID") if guild else None,
        allowed_user_ids=allowed,
        alert_channel_id=_int(env, "ALERT_CHANNEL_ID"),
        docker_host=env.get("DOCKER_HOST", "tcp://127.0.0.1:2375"),
        webhook_secret=secret,
        webhook_host=env.get("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=_int(env, "WEBHOOK_PORT", 8080),
        wol_hosts=_wol_hosts(env.get("WOL_HOSTS", "")),
        wol_broadcast=env.get("WOL_BROADCAST", "255.255.255.255"),
        lockdown_containers=lockdown,
        nut_host=env.get("NUT_HOST", "").strip() or None,
        nut_port=_int(env, "NUT_PORT", 3493),
        nut_ups=nut_ups,
        alert_cooldown_s=_int(env, "ALERT_COOLDOWN_S", 300),
        log_level=log_level,
    )
