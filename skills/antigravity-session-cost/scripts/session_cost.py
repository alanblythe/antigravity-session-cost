#!/usr/bin/env python3
"""Local, recursive session cost calculator for Antigravity CLI.

Computes estimated USD spend across a root conversation and all locally spawned
subagents / Swarm workers by incrementally parsing ``transcript.jsonl`` files
against a layered rate card (bundled list prices, local overrides, or a cached
remote GCS / HTTPS enterprise rate card with ETag validation).

Uses Python 3.11+ standard library only so it runs anywhere with zero external
dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Standard XDG / enterprise config paths (no hardcoded usernames)
DEFAULT_SYSTEM_CONFIG = Path("/etc/antigravity-session-cost/config.json")
DEFAULT_USER_CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
) / "antigravity-session-cost"
_INITIAL_USER_CONFIG_DIR = DEFAULT_USER_CONFIG_DIR
DEFAULT_CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
) / "antigravity-session-cost"

SKILL_ROOT = Path(os.path.realpath(__file__)).resolve().parent.parent
BUNDLED_RATE_CARD = SKILL_ROOT / "config" / "rate_card.json"

DEFAULT_TTL_SECONDS = 86400  # 24 hours
CACHE_VERSION = 2
DEFAULT_WARN_USD = 1.00
DEFAULT_HIGH_USD = 5.00
SURFACE_SUBDIRS = ("antigravity-cli", "antigravity", "antigravity-ide")
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


@dataclass
class ModelRates:
    key: str
    input_per_m: float
    output_per_m: float
    cache_read_per_m: float
    gt_200k_input: float | None = None
    gt_200k_output: float | None = None
    gt_200k_cache_read: float | None = None
    estimated: bool = False


@dataclass
class ModelUsageTotals:
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    steps: int = 0
    cost_usd: float = 0.0
    cache_savings_usd: float = 0.0
    estimated: bool = False
    rate_card_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "output_tokens": self.output_tokens,
            "steps": self.steps,
            "cost_usd": round(self.cost_usd, 6),
            "cache_savings_usd": round(self.cache_savings_usd, 6),
            "estimated": self.estimated,
            "rate_card_key": self.rate_card_key,
        }


@dataclass
class TranscriptDelta:
    offset: int = 0
    mtime_ns: int = 0
    size: int = 0
    child_ids: set[str] = field(default_factory=set)
    # List of (model_id, input_tokens, cache_read_tokens, output_tokens, created_at_iso)
    steps: list[tuple[str, int, int, int, str]] = field(default_factory=list)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically via temp file + os.replace to avoid partial reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json_safe(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        return None
    return None


def _resolve_user_config_dir(user_config_dir: Path | None = None) -> Path:
    """Resolve user config directory dynamically unless explicitly overridden."""
    if user_config_dir is not None:
        return user_config_dir
    if DEFAULT_USER_CONFIG_DIR != _INITIAL_USER_CONFIG_DIR:
        return DEFAULT_USER_CONFIG_DIR
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        / "antigravity-session-cost"
    )


def load_runtime_config(
    system_config: Path = DEFAULT_SYSTEM_CONFIG,
    user_config_dir: Path | None = None,
) -> dict[str, Any]:
    """Load merged config with 3-tier precedence: env > system/MDM > user."""
    resolved_user_cfg_dir = _resolve_user_config_dir(user_config_dir)
    merged: dict[str, Any] = {
        "rate_card_url": "",
        "rate_card_path": "",
        "rate_card_ttl_seconds": DEFAULT_TTL_SECONDS,
        "auth_mode": "auto",  # auto | adc | bearer | none
        "global_discount_pct": None,
        "gemini_dir": "",
        "overrides": {},
    }

    user_cfg = _read_json_safe(resolved_user_cfg_dir / "config.json") or {}
    sys_cfg = _read_json_safe(system_config) or {}

    for src in (user_cfg, sys_cfg):
        for key in ("rate_card_url", "rate_card_path", "auth_mode"):
            if src.get(key):
                merged[key] = src[key]
        raw_gemini_dir = src.get("gemini_dir")
        if isinstance(raw_gemini_dir, str) and raw_gemini_dir.strip():
            merged["gemini_dir"] = raw_gemini_dir.strip()
        if isinstance(src.get("rate_card_ttl_seconds"), (int, float)):
            merged["rate_card_ttl_seconds"] = int(src["rate_card_ttl_seconds"])
        if src.get("global_discount_pct") is not None:
            merged["global_discount_pct"] = float(src["global_discount_pct"])
        if isinstance(src.get("overrides"), dict):
            merged["overrides"].update(src["overrides"])

    if os.environ.get("AGY_COST_RATE_CARD_URL"):
        merged["rate_card_url"] = os.environ["AGY_COST_RATE_CARD_URL"].strip()
    if os.environ.get("AGY_COST_RATE_CARD"):
        merged["rate_card_path"] = os.environ["AGY_COST_RATE_CARD"].strip()
    if os.environ.get("AGY_COST_GEMINI_DIR", "").strip():
        merged["gemini_dir"] = os.environ["AGY_COST_GEMINI_DIR"].strip()
    elif os.environ.get("GEMINI_DIR", "").strip():
        merged["gemini_dir"] = os.environ["GEMINI_DIR"].strip()
    if os.environ.get("AGY_COST_RATE_CARD_TTL"):
        try:
            merged["rate_card_ttl_seconds"] = int(os.environ["AGY_COST_RATE_CARD_TTL"])
        except ValueError:
            pass
    if os.environ.get("AGY_COST_DISCOUNT_PCT"):
        try:
            merged["global_discount_pct"] = float(os.environ["AGY_COST_DISCOUNT_PCT"])
        except ValueError:
            pass

    return merged


def _gcs_uri_to_json_api_url(uri: str) -> str:
    """Convert gs://bucket/path/to/rate_card.json to the GCS JSON media URL."""
    stripped = uri[len("gs://") :]
    if "/" not in stripped:
        raise ValueError(f"Invalid GCS URI (missing object path): {uri}")
    bucket, obj = stripped.split("/", 1)
    encoded_obj = urllib.parse.quote(obj, safe="")
    return f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{encoded_obj}?alt=media"


def _get_adc_access_token(timeout: float = 4.0) -> str | None:
    """Fetch a GCP ADC OAuth2 access token using gcloud if available."""
    for cmd in (
        ["gcloud", "auth", "application-default", "print-access-token"],
        ["gcloud", "auth", "print-access-token"],
    ):
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            token = res.stdout.strip()
            if res.returncode == 0 and token:
                return token
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _get_adc_identity_token(timeout: float = 4.0) -> str | None:
    try:
        res = subprocess.run(
            ["gcloud", "auth", "print-identity-token"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        token = res.stdout.strip()
        if res.returncode == 0 and token:
            return token
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _build_remote_headers(url: str, is_gcs: bool, auth_mode: str, etag: str | None) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag

    custom_header = os.environ.get("AGY_COST_RATE_CARD_AUTH_HEADER", "").strip()
    bearer_token = os.environ.get("AGY_COST_RATE_CARD_TOKEN", "").strip()

    if custom_header and ":" in custom_header:
        k, v = custom_header.split(":", 1)
        headers[k.strip()] = v.strip()
    elif bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    elif auth_mode == "adc" or (auth_mode == "auto" and is_gcs):
        token = _get_adc_access_token() if is_gcs else _get_adc_identity_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

    return headers


def refresh_remote_rate_card(
    url: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    auth_mode: str = "auto",
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """Fetch a remote GCS or HTTPS rate card using ETag validation.

    Returns (updated_or_valid, status_message). Never raises on network failure
    so callers can fail open to cached or bundled pricing.
    """
    cache_path = cache_dir / "rate_card.json"
    meta_path = cache_dir / "rate_card.meta.json"
    meta = _read_json_safe(meta_path) or {}

    is_gcs = url.startswith("gs://")
    try:
        fetch_url = _gcs_uri_to_json_api_url(url) if is_gcs else url
    except ValueError as exc:
        return False, str(exc)

    etag = meta.get("etag") if meta.get("url") == url else None
    headers = _build_remote_headers(fetch_url, is_gcs=is_gcs, auth_mode=auth_mode, etag=etag)

    req = urllib.request.Request(fetch_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body)
            if not isinstance(parsed, dict) or "models" not in parsed:
                return False, "Remote rate card JSON missing 'models' map"
            new_etag = resp.headers.get("ETag") or resp.headers.get("etag") or ""
            _atomic_write_json(cache_path, parsed)
            _atomic_write_json(
                meta_path,
                {
                    "url": url,
                    "etag": new_etag,
                    "fetched_at": time.time(),
                    "status": 200,
                },
            )
            return True, f"Updated rate card from {url} (ETag: {new_etag or 'none'})"
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            _atomic_write_json(
                meta_path,
                {
                    "url": url,
                    "etag": etag or "",
                    "fetched_at": time.time(),
                    "status": 304,
                },
            )
            return True, f"Rate card unchanged (304 Not Modified) for {url}"
        return False, f"HTTP {exc.code} fetching {url}"
    except Exception as exc:
        return False, f"Failed to fetch {url}: {exc}"


def load_rate_card(
    config: dict[str, Any] | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    user_config_dir: Path | None = None,
    allow_network: bool = False,
) -> tuple[dict[str, Any], str]:
    """Resolve the active rate card without blocking statusLine renders.

    Precedence:
      1. Explicit local file (`rate_card_path` / `AGY_COST_RATE_CARD`)
      2. Cached remote rate card (refreshed only if `allow_network=True` & TTL expired)
      3. User local rate card (`~/.config/antigravity-session-cost/rate_card.json`)
      4. Bundled Vertex AI list-price rate card
    """
    resolved_user_cfg_dir = _resolve_user_config_dir(user_config_dir)
    cfg = config if config is not None else load_runtime_config(user_config_dir=resolved_user_cfg_dir)
    card: dict[str, Any] | None = None
    source = "bundled"

    explicit_path = cfg.get("rate_card_path")
    if explicit_path:
        expanded = Path(os.path.expanduser(explicit_path))
        card = _read_json_safe(expanded)
        if card:
            source = str(expanded)

    remote_url = cfg.get("rate_card_url")
    if card is None and remote_url:
        cache_path = cache_dir / "rate_card.json"
        meta = _read_json_safe(cache_dir / "rate_card.meta.json") or {}
        ttl = int(cfg.get("rate_card_ttl_seconds", DEFAULT_TTL_SECONDS))
        age = time.time() - float(meta.get("fetched_at", 0.0))
        if allow_network and (not cache_path.is_file() or meta.get("url") != remote_url or age >= ttl):
            refresh_remote_rate_card(
                remote_url,
                cache_dir=cache_dir,
                auth_mode=str(cfg.get("auth_mode", "auto")),
            )
        cached = _read_json_safe(cache_path)
        if cached:
            card = cached
            source = f"cached:{remote_url}"

    if card is None:
        user_card = _read_json_safe(resolved_user_cfg_dir / "rate_card.json")
        if user_card:
            card = user_card
            source = str(resolved_user_cfg_dir / "rate_card.json")

    if card is None:
        card = _read_json_safe(BUNDLED_RATE_CARD) or {
            "global_discount_pct": 0.0,
            "default_tier_threshold_tokens": 200000,
            "models": {
                "default_fallback": {
                    "match": ["*"],
                    "input": 2.0,
                    "output": 10.0,
                    "cache_read": 0.1,
                }
            },
            "overrides": {},
        }
        source = "bundled"

    # Merge config-level discount / overrides without mutating the source file
    merged_card = json.loads(json.dumps(card))
    if cfg.get("global_discount_pct") is not None:
        merged_card["global_discount_pct"] = float(cfg["global_discount_pct"])
    if cfg.get("overrides"):
        merged_overrides = dict(merged_card.get("overrides") or {})
        merged_overrides.update(cfg["overrides"])
        merged_card["overrides"] = merged_overrides

    return merged_card, source


def resolve_model_rates(model_id: str, rate_card: dict[str, Any]) -> ModelRates:
    """Resolve effective per-1M token rates for a model, applying overrides and discounts."""
    norm = (model_id or "").lower().strip()
    short = norm.rsplit("/", 1)[-1]
    models: dict[str, Any] = rate_card.get("models") or {}
    overrides: dict[str, Any] = rate_card.get("overrides") or {}
    global_discount = max(0.0, min(100.0, float(rate_card.get("global_discount_pct") or 0.0)))

    matched_key: str | None = None
    matched_entry: dict[str, Any] | None = None
    estimated = False

    # 1. Exact key match (models or overrides)
    for candidate in (norm, short):
        if candidate in overrides and isinstance(overrides[candidate], dict):
            matched_key = candidate
            base = models.get(candidate) or {}
            matched_entry = {**base, **overrides[candidate]}
            break
        if candidate in models and isinstance(models[candidate], dict):
            matched_key = candidate
            matched_entry = dict(models[candidate])
            break

    # 2. Substring pattern match sorted by longest pattern first
    if matched_entry is None:
        patterns: list[tuple[int, str, dict[str, Any]]] = []
        for k, entry in models.items():
            if k == "default_fallback" or not isinstance(entry, dict):
                continue
            for pat in entry.get("match") or [k]:
                p_low = str(pat).lower().strip()
                if p_low and p_low != "*":
                    patterns.append((len(p_low), k, entry))
        patterns.sort(key=lambda item: item[0], reverse=True)
        for _, k, entry in patterns:
            match_list = [str(p).lower().strip() for p in (entry.get("match") or [k])]
            if any(p and p != "*" and (p in short or p in norm) for p in match_list):
                matched_key = k
                matched_entry = dict(entry)
                if k in overrides and isinstance(overrides[k], dict):
                    matched_entry.update(overrides[k])
                break

    # 3. Fallback
    if matched_entry is None:
        matched_key = "default_fallback"
        matched_entry = dict(models.get("default_fallback") or {"input": 2.0, "output": 10.0, "cache_read": 0.1})
        estimated = True

    model_discount = matched_entry.get("discount_pct")
    discount_pct = float(model_discount) if model_discount is not None else global_discount
    discount_factor = max(0.0, 1.0 - (max(0.0, min(100.0, discount_pct)) / 100.0))

    def _scaled(field_name: str, default: float | None = None) -> float | None:
        val = matched_entry.get(field_name)
        if val is None:
            return default
        return float(val) * discount_factor

    return ModelRates(
        key=matched_key or "default_fallback",
        input_per_m=float(_scaled("input", 0.0) or 0.0),
        output_per_m=float(_scaled("output", 0.0) or 0.0),
        cache_read_per_m=float(_scaled("cache_read", 0.0) or 0.0),
        gt_200k_input=_scaled("gt_200k_input"),
        gt_200k_output=_scaled("gt_200k_output"),
        gt_200k_cache_read=_scaled("gt_200k_cache_read"),
        estimated=estimated,
    )


def price_step_detail(
    model_id: str,
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    rate_card: dict[str, Any],
) -> tuple[float, float, ModelRates]:
    """Calculate USD cost and prompt-cache savings for a single PLANNER_RESPONSE step.

    In Antigravity transcripts, `input_tokens` and `cache_read_tokens` are
    mutually exclusive (total prompt = input_tokens + cache_read_tokens), and
    `output_tokens` includes thinking + response output tokens.
    """
    rates = resolve_model_rates(model_id, rate_card)
    threshold = int(rate_card.get("default_tier_threshold_tokens") or 200000)
    in_tok = max(0, input_tokens)
    cache_tok = max(0, cache_read_tokens)
    out_tok = max(0, output_tokens)
    total_prompt = in_tok + cache_tok
    use_gt_tier = total_prompt > threshold

    in_rate = (
        rates.gt_200k_input
        if (use_gt_tier and rates.gt_200k_input is not None)
        else rates.input_per_m
    )
    out_rate = (
        rates.gt_200k_output
        if (use_gt_tier and rates.gt_200k_output is not None)
        else rates.output_per_m
    )
    cache_rate = (
        rates.gt_200k_cache_read
        if (use_gt_tier and rates.gt_200k_cache_read is not None)
        else rates.cache_read_per_m
    )

    cost = (
        (in_tok * in_rate)
        + (cache_tok * cache_rate)
        + (out_tok * out_rate)
    ) / 1_000_000.0
    cache_savings = max(0.0, (in_rate - cache_rate) * cache_tok / 1_000_000.0)
    return cost, cache_savings, rates


def price_step(
    model_id: str,
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    rate_card: dict[str, Any],
) -> tuple[float, ModelRates]:
    """Calculate USD cost for a single PLANNER_RESPONSE step."""
    cost, _, rates = price_step_detail(
        model_id, input_tokens, cache_read_tokens, output_tokens, rate_card
    )
    return cost, rates


def _extract_child_ids_from_step(step: dict[str, Any]) -> set[str]:
    """Extract spawned subagent / Swarm worker conversation IDs from a transcript line."""
    found: set[str] = set()

    # Tool responses / generic step content for invoke_subagent or swarm often include conversationId
    content = step.get("content")
    if isinstance(content, str) and (
        "conversationId" in content
        or "conversation_id" in content
        or "subagent" in content.lower()
        or "swarm" in content.lower()
    ):
        for match in UUID_RE.findall(content):
            found.add(match.lower())

    tool_calls = step.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "").lower()
            if name in ("invoke_subagent", "send_message", "swarm"):
                raw = json.dumps(call)
                for match in UUID_RE.findall(raw):
                    found.add(match.lower())

    return found


def _transcript_cache_path(transcript_path: Path, cache_dir: Path) -> Path:
    digest = hashlib.sha256(str(transcript_path.resolve()).encode("utf-8")).hexdigest()[:16]
    return cache_dir / "transcripts" / f"{digest}.json"


def _read_transcript_incremental(
    transcript_path: Path,
    default_model: str,
    cache_dir: Path,
) -> TranscriptDelta:
    """Incrementally parse transcript.jsonl using a byte-offset cache."""
    if not transcript_path.is_file():
        return TranscriptDelta()

    stat = transcript_path.stat()
    cpath = _transcript_cache_path(transcript_path, cache_dir)
    cached = _read_json_safe(cpath)

    delta = TranscriptDelta()
    start_offset = 0

    if (
        cached
        and cached.get("cache_version") == CACHE_VERSION
        and cached.get("path") == str(transcript_path.resolve())
        and int(cached.get("offset", 0)) <= stat.st_size
    ):
        start_offset = int(cached.get("offset", 0))
        delta.offset = start_offset
        delta.mtime_ns = int(cached.get("mtime_ns", 0))
        delta.size = int(cached.get("size", 0))
        delta.child_ids = set(cached.get("child_ids") or [])
        delta.steps = [
            (str(s[0]), int(s[1]), int(s[2]), int(s[3]), str(s[4]))
            for s in (cached.get("steps") or [])
            if isinstance(s, list) and len(s) == 5
        ]
        if start_offset == stat.st_size and delta.mtime_ns == stat.st_mtime_ns:
            return delta

    new_offset = start_offset
    with transcript_path.open("rb") as handle:
        handle.seek(start_offset)
        while True:
            raw_line = handle.readline()
            if not raw_line:
                break
            # Only advance offset over complete newline-terminated lines
            if not raw_line.endswith(b"\n"):
                break
            new_offset += len(raw_line)
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                step = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(step, dict):
                continue

            delta.child_ids.update(_extract_child_ids_from_step(step))

            if step.get("type") == "PLANNER_RESPONSE":
                in_tok = int(step.get("input_tokens") or 0)
                cache_tok = int(step.get("cache_read_tokens") or 0)
                out_tok = int(step.get("output_tokens") or 0)
                if in_tok > 0 or cache_tok > 0 or out_tok > 0:
                    step_model = str(step.get("model") or default_model or "")
                    created_at_iso = str(step.get("created_at") or "")
                    delta.steps.append((step_model, in_tok, cache_tok, out_tok, created_at_iso))

    delta.offset = new_offset
    delta.mtime_ns = stat.st_mtime_ns
    delta.size = stat.st_size

    try:
        _atomic_write_json(
            cpath,
            {
                "cache_version": CACHE_VERSION,
                "path": str(transcript_path.resolve()),
                "offset": delta.offset,
                "mtime_ns": delta.mtime_ns,
                "size": delta.size,
                "child_ids": sorted(delta.child_ids),
                "steps": [list(s) for s in delta.steps],
            },
        )
    except OSError:
        pass

    return delta


def _resolve_brain_root(transcript_path: Path) -> tuple[Path | None, str | None]:
    """Infer <appDataDir>/brain and current conversation ID from transcript_path."""
    parts = transcript_path.resolve().parts
    for idx in range(len(parts) - 1, -1, -1):
        if parts[idx] == "brain" and idx + 1 < len(parts):
            brain_root = Path(*parts[: idx + 1])
            conv_id = parts[idx + 1].lower()
            return brain_root, conv_id
    return None, None


def compute_session_cost(
    transcript_path: Path,
    rate_card: dict[str, Any],
    default_model: str = "",
    cache_dir: Path = DEFAULT_CACHE_DIR,
    max_depth: int = 4,
) -> dict[str, Any]:
    """Recursively compute session cost across root + local child conversations."""
    brain_root, root_conv_id = _resolve_brain_root(transcript_path)
    visited: set[Path] = set()

    main_usd = 0.0
    subagent_usd = 0.0
    subagent_count = 0
    total_cache_savings = 0.0
    total_in = 0
    total_cache = 0
    total_out = 0
    total_steps = 0
    any_estimated = False
    model_totals: dict[str, ModelUsageTotals] = {}

    def _walk(path: Path, is_root: bool, depth: int) -> None:
        nonlocal main_usd, subagent_usd, subagent_count, total_cache_savings
        nonlocal total_in, total_cache, total_out, total_steps, any_estimated
        resolved = path.resolve()
        if resolved in visited or not resolved.is_file() or depth > max_depth:
            return
        visited.add(resolved)
        if not is_root:
            subagent_count += 1

        delta = _read_transcript_incremental(resolved, default_model=default_model, cache_dir=cache_dir)
        for model_id, in_tok, cache_tok, out_tok, _created_at in delta.steps:
            step_cost, step_savings, rates = price_step_detail(
                model_id, in_tok, cache_tok, out_tok, rate_card
            )
            if rates.estimated:
                any_estimated = True
            if is_root:
                main_usd += step_cost
            else:
                subagent_usd += step_cost
            total_cache_savings += step_savings
            total_in += in_tok
            total_cache += cache_tok
            total_out += out_tok
            total_steps += 1

            display_model = model_id.strip() or rates.key
            bucket = model_totals.setdefault(
                display_model,
                ModelUsageTotals(estimated=rates.estimated, rate_card_key=rates.key),
            )
            bucket.input_tokens += in_tok
            bucket.cache_read_tokens += cache_tok
            bucket.output_tokens += out_tok
            bucket.steps += 1
            bucket.cost_usd += step_cost
            bucket.cache_savings_usd += step_savings

        if brain_root is not None:
            for cid in sorted(delta.child_ids):
                if cid == root_conv_id:
                    continue
                child_transcript = brain_root / cid / ".system_generated" / "logs" / "transcript.jsonl"
                if child_transcript.is_file():
                    _walk(child_transcript, is_root=False, depth=depth + 1)

    _walk(transcript_path, is_root=True, depth=0)

    total_usd = main_usd + subagent_usd
    return {
        "total_usd": round(total_usd, 6),
        "main_usd": round(main_usd, 6),
        "subagent_usd": round(subagent_usd, 6),
        "subagent_count": subagent_count,
        "cache_savings_usd": round(total_cache_savings, 6),
        "input_tokens": total_in,
        "cache_read_tokens": total_cache,
        "output_tokens": total_out,
        "steps": total_steps,
        "estimated": any_estimated,
        "models": {k: v.to_dict() for k, v in sorted(model_totals.items())},
    }


def _parse_step_timestamp(created_at_iso: str, fallback_local: datetime) -> datetime:
    """Parse an ISO-8601 timestamp into the user's local timezone."""
    raw = (created_at_iso or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone()
        except ValueError:
            pass
    return fallback_local


def _read_linux_proc(pid: int, proc_root: Path = Path("/proc")) -> tuple[int | None, list[str]] | None:
    """Read (ppid, argv) for ``pid`` from Linux ``/proc``."""
    stat_path = proc_root / str(pid) / "stat"
    cmdline_path = proc_root / str(pid) / "cmdline"
    if not stat_path.is_file():
        return None
    try:
        stat_text = stat_path.read_text(encoding="utf-8", errors="replace")
        rparen = stat_text.rfind(")")
        if rparen == -1:
            return None
        fields = stat_text[rparen + 1 :].split()
        if len(fields) < 2:
            return None
        ppid = int(fields[1])
    except (OSError, ValueError):
        return None

    argv: list[str] = []
    try:
        raw_cmd = cmdline_path.read_bytes()
        if raw_cmd:
            argv = [
                part.decode("utf-8", errors="replace")
                for part in raw_cmd.split(b"\x00")
                if part
            ]
    except OSError:
        argv = []

    return ppid, argv


def _read_ps_proc(pid: int) -> tuple[int | None, list[str]] | None:
    """Fallback/macOS process inspector using ``ps -o ppid=,args= -p <pid>``."""
    try:
        res = subprocess.run(
            ["ps", "-o", "ppid=,args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    parts = lines[0].split(None, 1)
    try:
        ppid = int(parts[0])
    except ValueError:
        return None
    args_str = parts[1].strip() if len(parts) > 1 else ""
    if not args_str:
        return ppid, []
    try:
        argv = shlex.split(args_str)
    except ValueError:
        argv = args_str.split()
    return ppid, argv


def _read_process_info(pid: int) -> tuple[int | None, list[str]] | None:
    """Read (ppid, argv) using Linux /proc when available, else ``ps``."""
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        info = _read_linux_proc(pid)
        if info is not None:
            return info
    return _read_ps_proc(pid)


def _extract_flag_value(args: list[str], flag_names: tuple[str, ...]) -> str | None:
    """Extract a CLI flag value from argv (supports ``--flag val`` and ``--flag=val``)."""
    found: str | None = None
    for idx, arg in enumerate(args):
        for name in flag_names:
            if arg == name and idx + 1 < len(args):
                val = args[idx + 1].strip()
                if val:
                    found = val
            elif arg.startswith(f"{name}="):
                val = arg.split("=", 1)[1].strip()
                if val:
                    found = val
    return found


def detect_active_gemini_dir(start_pid: int | None = None) -> Path | None:
    """Detect the active Antigravity config root from ancestor processes.

    Walks the ancestor PID chain from ``start_pid`` (defaults to ``os.getppid()``)
    upwards for at most 16 hops. If an ancestor executable basename is ``agy`` or
    starts with ``language_server``, returns its ``--gemini_dir`` (or parent of
    ``--config_dir`` when named ``config``), or ``~/.gemini`` when launched
    without a custom directory flag. Returns ``None`` in standalone shells.
    """
    pid: int | None = os.getppid() if start_pid is None else start_pid
    visited: set[int] = set()

    for _ in range(16):
        if pid is None or pid <= 0 or pid in visited:
            break
        visited.add(pid)

        info = _read_process_info(pid)
        if info is None:
            break
        ppid, argv = info

        if argv:
            exe_name = Path(argv[0]).name
            if exe_name == "agy" or exe_name.startswith("language_server"):
                gemini_flag = _extract_flag_value(
                    argv[1:], ("--gemini_dir", "--gemini-dir")
                )
                if gemini_flag:
                    return Path(os.path.expanduser(gemini_flag)).resolve()

                config_flag = _extract_flag_value(
                    argv[1:], ("--config_dir", "--config-dir")
                )
                if config_flag:
                    cfg_path = Path(os.path.expanduser(config_flag))
                    if cfg_path.name == "config":
                        return cfg_path.parent.resolve()

                return (Path.home() / ".gemini").resolve()

        pid = ppid

    return None


def resolve_brain_dirs(
    brain_dir: str | None = None,
    gemini_dir: str | None = None,
    transcript_path: str | None = None,
    all_surfaces: bool = False,
    config: dict[str, Any] | None = None,
    user_config_dir: Path | None = None,
) -> list[Path]:
    """Resolve brain directories for the single active Antigravity root.

    Precedence (strictly resolves one root; never merges ~/.gemini + ~/.antigravity):
      1. Explicit --brain-dir
      2. Explicit --transcript parent brain/ (when not --all-surfaces)
      3. Explicit --gemini-dir CLI flag or AGY_COST_GEMINI_DIR / GEMINI_DIR env var
      4. Active ancestor process (`detect_active_gemini_dir()`)
      5. Configured gemini_dir in config.json (standalone shell fallback)
      6. Default Path.home() / ".gemini" (single deterministic fallback)
    """
    if brain_dir:
        return [Path(os.path.expanduser(brain_dir))]

    if transcript_path and not all_surfaces:
        inferred, _ = _resolve_brain_root(Path(os.path.expanduser(transcript_path)))
        if inferred is not None:
            return [inferred]

    env_gemini_dir = (
        os.environ.get("AGY_COST_GEMINI_DIR", "").strip()
        or os.environ.get("GEMINI_DIR", "").strip()
    )
    explicit_gemini_dir = (gemini_dir or "").strip() or env_gemini_dir

    root: Path
    if explicit_gemini_dir:
        root = Path(os.path.expanduser(explicit_gemini_dir))
    else:
        active_dir = detect_active_gemini_dir()
        if active_dir is not None:
            root = active_dir
        else:
            cfg = (
                config
                if config is not None
                else load_runtime_config(user_config_dir=user_config_dir)
            )
            cfg_gemini_dir = str(cfg.get("gemini_dir") or "").strip()
            if cfg_gemini_dir:
                root = Path(os.path.expanduser(cfg_gemini_dir))
            else:
                root = Path.home() / ".gemini"

    if all_surfaces:
        candidates: list[Path] = []
        seen: set[Path] = set()
        for surface in SURFACE_SUBDIRS:
            p = root / surface / "brain"
            if p.is_dir():
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    candidates.append(p)
        if not candidates:
            candidates.append(root / "antigravity-cli" / "brain")
        return candidates

    return [root / "antigravity-cli" / "brain"]


def compute_historical_report(
    brain_dirs: list[Path],
    rate_card: dict[str, Any],
    window: str = "7d",
    default_model: str = "",
    cache_dir: Path = DEFAULT_CACHE_DIR,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Scan brain directories flat (once per transcript) for historical spend.

    Guarantees zero double-counting of subagents / Swarm workers by reading each
    transcript.jsonl exactly once while tracking referenced child conversation IDs
    to distinguish top-level sessions from spawned subagent transcripts.
    """
    now_local = now.astimezone() if now is not None else datetime.now().astimezone()
    start_of_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    window_days = {"1d": 1, "7d": 7, "30d": 30}.get(window)
    cutoff_local = (
        start_of_today - timedelta(days=window_days - 1) if window_days is not None else None
    )

    seen_files: set[Path] = set()
    transcripts: list[tuple[str, Path, TranscriptDelta, datetime]] = []
    refs_by_conv: dict[str, set[str]] = {}
    earliest_by_conv: dict[str, datetime] = {}

    for bdir in brain_dirs:
        if not bdir.is_dir():
            continue
        try:
            entries = sorted(bdir.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for conv_dir in entries:
            if not conv_dir.is_dir():
                continue
            tpath = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if not tpath.is_file():
                continue
            resolved = tpath.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            conv_id = conv_dir.name.lower()
            delta = _read_transcript_incremental(
                resolved, default_model=default_model, cache_dir=cache_dir
            )
            try:
                mtime = resolved.stat().st_mtime
            except OSError:
                mtime = now_local.timestamp()
            fallback_dt = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone()
            step_dts = [_parse_step_timestamp(s[4], fallback_dt) for s in delta.steps]
            earliest_dt = min(step_dts) if step_dts else fallback_dt

            transcripts.append((conv_id, resolved, delta, fallback_dt))
            refs_by_conv[conv_id] = {cid for cid in delta.child_ids if cid != conv_id}
            earliest_by_conv[conv_id] = earliest_dt

    # Identify spawned child transcripts. When two transcripts mutually reference each
    # other (e.g. parent invokes subagent and subagent replies via send_message), keep
    # the earlier-started transcript as top-level and mark only the later one as child.
    all_child_ids: set[str] = set()
    for conv_id, refs in refs_by_conv.items():
        for cid in refs:
            if cid in refs_by_conv and conv_id in refs_by_conv[cid]:
                if earliest_by_conv[conv_id] <= earliest_by_conv[cid]:
                    all_child_ids.add(cid)
            else:
                all_child_ids.add(cid)

    daily_map: dict[str, dict[str, Any]] = {}
    model_totals: dict[str, ModelUsageTotals] = {}
    active_top_level: set[str] = set()
    active_subagents: set[str] = set()

    total_in = 0
    total_cache = 0
    total_out = 0
    total_steps = 0
    total_usd = 0.0
    total_savings = 0.0
    any_estimated = False

    for conv_id, _resolved, delta, fallback_dt in transcripts:
        is_top_level = conv_id not in all_child_ids

        for model_id, in_tok, cache_tok, out_tok, created_at_iso in delta.steps:
            step_dt = _parse_step_timestamp(created_at_iso, fallback_dt)
            if cutoff_local is not None and step_dt < cutoff_local:
                continue

            step_cost, step_savings, rates = price_step_detail(
                model_id, in_tok, cache_tok, out_tok, rate_card
            )
            if rates.estimated:
                any_estimated = True

            if is_top_level:
                active_top_level.add(conv_id)
            else:
                active_subagents.add(conv_id)

            day_key = step_dt.strftime("%Y-%m-%d")
            day_bucket = daily_map.setdefault(
                day_key,
                {
                    "date": day_key,
                    "_top_sessions": set(),
                    "_sub_sessions": set(),
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "steps": 0,
                    "cost_usd": 0.0,
                    "cache_savings_usd": 0.0,
                    "estimated": False,
                },
            )
            if is_top_level:
                day_bucket["_top_sessions"].add(conv_id)
            else:
                day_bucket["_sub_sessions"].add(conv_id)
            day_bucket["input_tokens"] += in_tok
            day_bucket["cache_read_tokens"] += cache_tok
            day_bucket["output_tokens"] += out_tok
            day_bucket["steps"] += 1
            day_bucket["cost_usd"] += step_cost
            day_bucket["cache_savings_usd"] += step_savings
            if rates.estimated:
                day_bucket["estimated"] = True

            display_model = model_id.strip() or rates.key
            mbucket = model_totals.setdefault(
                display_model,
                ModelUsageTotals(estimated=rates.estimated, rate_card_key=rates.key),
            )
            mbucket.input_tokens += in_tok
            mbucket.cache_read_tokens += cache_tok
            mbucket.output_tokens += out_tok
            mbucket.steps += 1
            mbucket.cost_usd += step_cost
            mbucket.cache_savings_usd += step_savings

            total_in += in_tok
            total_cache += cache_tok
            total_out += out_tok
            total_steps += 1
            total_usd += step_cost
            total_savings += step_savings

    daily_rows: list[dict[str, Any]] = []
    for day_key in sorted(daily_map.keys()):
        b = daily_map[day_key]
        daily_rows.append(
            {
                "date": day_key,
                "sessions": len(b["_top_sessions"]),
                "subagent_transcripts": len(b["_sub_sessions"]),
                "input_tokens": b["input_tokens"],
                "cache_read_tokens": b["cache_read_tokens"],
                "output_tokens": b["output_tokens"],
                "steps": b["steps"],
                "cost_usd": round(b["cost_usd"], 6),
                "cache_savings_usd": round(b["cache_savings_usd"], 6),
                "estimated": b["estimated"],
            }
        )

    sorted_models = dict(
        sorted(model_totals.items(), key=lambda kv: (-kv[1].cost_usd, kv[0]))
    )
    return {
        "window": window,
        "brain_dirs": [str(p) for p in brain_dirs],
        "total_sessions": len(active_top_level),
        "subagent_transcripts": len(active_subagents),
        "input_tokens": total_in,
        "cache_read_tokens": total_cache,
        "output_tokens": total_out,
        "steps": total_steps,
        "total_usd": round(total_usd, 6),
        "cache_savings_usd": round(total_savings, 6),
        "estimated": any_estimated,
        "daily": daily_rows,
        "models": {k: v.to_dict() for k, v in sorted_models.items()},
    }


def _fmt_tokens(count: int) -> str:
    if count >= 10_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def _fmt_usd(amount: float, estimated: bool = False) -> str:
    prefix = "~$" if estimated else "$"
    if amount >= 0.01 or amount == 0.0:
        return f"{prefix}{amount:.2f}"
    return f"{prefix}{amount:.4f}"


def _fmt_hit_pct(input_tokens: int, cache_read_tokens: int) -> str:
    denom = input_tokens + cache_read_tokens
    pct = (cache_read_tokens / denom * 100.0) if denom > 0 else 0.0
    return f"{pct:.1f}%"


def _render_markdown_table(
    headers: list[str],
    rows: list[list[str]],
    right_align: set[int] | None = None,
) -> str:
    align = right_align or set()
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def _fmt_row(cells: list[str]) -> str:
        formatted = []
        for idx, cell in enumerate(cells):
            if idx in align:
                formatted.append(cell.rjust(widths[idx]))
            else:
                formatted.append(cell.ljust(widths[idx]))
        return "| " + " | ".join(formatted) + " |"

    sep_cells = []
    for idx, w in enumerate(widths):
        dashes = "-" * max(3, w)
        sep_cells.append((dashes[:-1] + ":") if idx in align else (":" + dashes[1:]))

    lines = [_fmt_row(headers), "| " + " | ".join(sep_cells) + " |"]
    for row in rows:
        lines.append(_fmt_row(row))
    return "\n".join(lines)


def _build_model_breakdown_table(models: dict[str, Any], total_usd: float) -> str:
    if not models:
        return "_No model usage recorded._"
    sorted_items = sorted(
        models.items(),
        key=lambda kv: (-float(kv[1].get("cost_usd") or 0.0), kv[0]),
    )
    rows: list[list[str]] = []
    for model_name, stats in sorted_items:
        in_tok = int(stats.get("input_tokens") or 0)
        cache_tok = int(stats.get("cache_read_tokens") or 0)
        out_tok = int(stats.get("output_tokens") or 0)
        steps = int(stats.get("steps") or 0)
        cost = float(stats.get("cost_usd") or 0.0)
        est = bool(stats.get("estimated", False))
        share = (cost / total_usd * 100.0) if total_usd > 0 else 0.0
        rows.append(
            [
                model_name,
                str(steps),
                f"{_fmt_tokens(in_tok)} / {_fmt_tokens(out_tok)}",
                f"{_fmt_tokens(cache_tok)} ({_fmt_hit_pct(in_tok, cache_tok)})",
                _fmt_usd(cost, estimated=est),
                f"{share:.1f}%",
            ]
        )
    return _render_markdown_table(
        ["Model", "Steps", "Input / Output", "Cache Read (Hit%)", "Cost (USD)", "Share"],
        rows,
        right_align={1, 2, 3, 4, 5},
    )


def format_report(report: dict[str, Any]) -> str:
    """Format a historical spend report as a readable Markdown table summary."""
    window = str(report.get("window") or "7d")
    daily = report.get("daily") or []
    total_sessions = int(report.get("total_sessions") or 0)
    sub_count = int(report.get("subagent_transcripts") or 0)
    total_in = int(report.get("input_tokens") or 0)
    total_cache = int(report.get("cache_read_tokens") or 0)
    total_usd = float(report.get("total_usd") or 0.0)
    savings = float(report.get("cache_savings_usd") or 0.0)
    estimated = bool(report.get("estimated", False))

    parts: list[str] = [f"### Antigravity Spend Report ({window})", ""]
    if daily:
        daily_rows = [
            [
                str(d["date"]),
                str(d["sessions"]),
                f"{_fmt_tokens(int(d['input_tokens']))} / {_fmt_tokens(int(d['output_tokens']))}",
                f"{_fmt_tokens(int(d['cache_read_tokens']))} ({_fmt_hit_pct(int(d['input_tokens']), int(d['cache_read_tokens']))})",
                _fmt_usd(float(d["cost_usd"]), estimated=bool(d.get("estimated", False))),
            ]
            for d in daily
        ]
        parts.append(
            _render_markdown_table(
                ["Date", "Sessions", "Input / Output", "Cache Read (Hit%)", "Cost (USD)"],
                daily_rows,
                right_align={1, 2, 3, 4},
            )
        )
    else:
        parts.append("_No activity recorded in this window._")

    sub_note = f" (+{sub_count} subagents)" if sub_count > 0 else ""
    parts.append("")
    parts.append(
        f"**Summary:** {total_sessions} sessions{sub_note} | "
        f"{_fmt_tokens(total_cache)} cached ({_fmt_hit_pct(total_in, total_cache)}) | "
        f"{_fmt_usd(total_usd, estimated=estimated)} "
        f"(saved ~{_fmt_usd(savings)} via prompt caching)"
    )

    models = report.get("models") or {}
    if models:
        parts.extend(["", "#### Model Breakdown", "", _build_model_breakdown_table(models, total_usd)])

    return "\n".join(parts)


def format_session_breakdown(result: dict[str, Any]) -> str:
    """Format a concise single-session cost breakdown with per-model table."""
    total_usd = float(result.get("total_usd") or 0.0)
    main_usd = float(result.get("main_usd") or 0.0)
    sub_usd = float(result.get("subagent_usd") or 0.0)
    sub_count = int(result.get("subagent_count") or 0)
    savings = float(result.get("cache_savings_usd") or 0.0)
    total_in = int(result.get("input_tokens") or 0)
    total_cache = int(result.get("cache_read_tokens") or 0)
    total_out = int(result.get("output_tokens") or 0)
    estimated = bool(result.get("estimated", False))

    parts = [
        "### Session Cost Breakdown",
        "",
        f"**Total:** {_fmt_usd(total_usd, estimated=estimated)} | "
        f"**Main:** {_fmt_usd(main_usd)} | "
        f"**Subagents:** {_fmt_usd(sub_usd)} ({sub_count})",
        f"**Tokens:** {_fmt_tokens(total_in)} in / {_fmt_tokens(total_out)} out | "
        f"**Cache Read:** {_fmt_tokens(total_cache)} ({_fmt_hit_pct(total_in, total_cache)}) | "
        f"saved ~{_fmt_usd(savings)} via prompt caching",
    ]
    models = result.get("models") or {}
    if models:
        parts.extend(["", _build_model_breakdown_table(models, total_usd)])
    return "\n".join(parts)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def format_badge(
    result: dict[str, Any],
    ansi: bool = True,
    show_subagents: bool = True,
    warn_usd: float | None = None,
    high_usd: float | None = None,
) -> str:
    """Format a compact statusLine badge such as `$0.42` or `$0.42 (sub $0.11)`."""
    total = float(result.get("total_usd") or 0.0)
    sub = float(result.get("subagent_usd") or 0.0)
    prefix = "~$" if result.get("estimated") else "$"
    total_str = f"{prefix}{total:.2f}" if total >= 0.01 or total == 0.0 else f"{prefix}{total:.4f}"

    if show_subagents and sub > 0.0001:
        sub_str = f"${sub:.2f}" if sub >= 0.01 else f"${sub:.4f}"
        text = f"{total_str} (sub {sub_str})"
    else:
        text = total_str

    if not ansi:
        return text

    warn_thresh = DEFAULT_WARN_USD if warn_usd is None else warn_usd
    high_thresh = DEFAULT_HIGH_USD if high_usd is None else high_usd
    if warn_usd is None:
        warn_thresh = _env_float("AGY_COST_WARN_USD", DEFAULT_WARN_USD)
    if high_usd is None:
        high_thresh = _env_float("AGY_COST_HIGH_USD", DEFAULT_HIGH_USD)

    if total >= high_thresh:
        color = "\033[91m"  # bright red
    elif total >= warn_thresh:
        color = "\033[93m"  # bright yellow
    else:
        color = "\033[92m"  # bright green
    return f"{color}{text}\033[0m"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Calculate local Antigravity CLI session and historical spend from transcript.jsonl."
    )
    parser.add_argument(
        "--transcript",
        help="Path to transcript.jsonl (if omitted, reads statusLine JSON from stdin).",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Fallback model ID when transcript lines omit a 'model' field.",
    )
    parser.add_argument(
        "--rate-card",
        help="Explicit local rate_card.json path (overrides config).",
    )
    parser.add_argument(
        "--refresh-rates",
        action="store_true",
        help="Fetch/validate remote rate_card_url (GCS/HTTPS) via ETag and exit.",
    )
    parser.add_argument(
        "--rate-card-url",
        help="Remote GCS (gs://...) or HTTPS rate card URL to refresh.",
    )
    parser.add_argument(
        "--report",
        nargs="?",
        const="7d",
        choices=("1d", "7d", "30d", "all"),
        default=None,
        help="Generate a historical spend report across local brain transcripts (1d, 7d, 30d, or all).",
    )
    parser.add_argument(
        "--brain-dir",
        help="Explicit brain/ directory to scan for --report (overrides --gemini-dir detection).",
    )
    parser.add_argument(
        "--gemini-dir",
        help="Base Gemini/Antigravity config directory (overrides AGY_COST_GEMINI_DIR / GEMINI_DIR, active agy ancestor detection, config.json gemini_dir, and ~/.gemini default).",
    )
    parser.add_argument(
        "--all-surfaces",
        action="store_true",
        help="With --report, scan antigravity-cli/brain, antigravity/brain, and antigravity-ide/brain under the resolved config root.",
    )
    parser.add_argument(
        "--breakdown",
        action="store_true",
        help="Print a concise single-session breakdown (Main vs. Subagents, cache savings, per-model table).",
    )
    parser.add_argument(
        "--format",
        choices=("json", "badge", "plain", "report"),
        default=None,
        help="Output format: badge (ANSI), plain (no ANSI), report (Markdown table), or json.",
    )
    parser.add_argument(
        "--force-local",
        action="store_true",
        help="Always compute from transcript even if stdin provides non-zero native cost.total_usd.",
    )
    parser.add_argument(
        "--write-cache",
        help="Optional path to write the computed JSON summary (useful from Stop hooks).",
    )
    args = parser.parse_args(argv)

    cfg = load_runtime_config()
    if args.rate_card:
        cfg["rate_card_path"] = args.rate_card
    if args.rate_card_url:
        cfg["rate_card_url"] = args.rate_card_url

    if args.refresh_rates:
        url = cfg.get("rate_card_url")
        if not url:
            print("No rate_card_url configured.", file=sys.stderr)
            return 1
        ok, msg = refresh_remote_rate_card(url, auth_mode=str(cfg.get("auth_mode", "auto")))
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if args.report:
        rate_card, card_source = load_rate_card(config=cfg, allow_network=False)
        brain_dirs = resolve_brain_dirs(
            brain_dir=args.brain_dir,
            gemini_dir=args.gemini_dir,
            transcript_path=args.transcript,
            all_surfaces=args.all_surfaces,
            config=cfg,
        )
        report = compute_historical_report(
            brain_dirs=brain_dirs,
            rate_card=rate_card,
            window=args.report,
            default_model=args.model,
        )
        report["source"] = f"report:{card_source}"
        if args.write_cache:
            _atomic_write_json(Path(os.path.expanduser(args.write_cache)), report)
        out_format = args.format or "report"
        if out_format == "json":
            print(json.dumps(report, indent=2))
        else:
            print(format_report(report))
        return 0

    stdin_data: dict[str, Any] = {}
    if not args.transcript and not sys.stdin.isatty():
        raw_in = sys.stdin.read().strip()
        if raw_in:
            try:
                parsed_in = json.loads(raw_in)
                if isinstance(parsed_in, dict):
                    stdin_data = parsed_in
            except json.JSONDecodeError:
                pass

    # Prefer native non-zero cost.total_usd when populated by the CLI
    native_cost = stdin_data.get("cost")
    if (
        not args.force_local
        and not args.breakdown
        and isinstance(native_cost, dict)
        and float(native_cost.get("total_usd") or 0.0) > 0.0
    ):
        result = {
            "total_usd": round(float(native_cost.get("total_usd") or 0.0), 6),
            "main_usd": round(
                max(
                    0.0,
                    float(native_cost.get("total_usd") or 0.0)
                    - float(native_cost.get("subagent_usd") or 0.0),
                ),
                6,
            ),
            "subagent_usd": round(float(native_cost.get("subagent_usd") or 0.0), 6),
            "subagent_count": len(stdin_data.get("subagents") or []),
            "cache_savings_usd": 0.0,
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 0,
            "steps": 0,
            "estimated": bool(native_cost.get("estimated", False)),
            "source": "native",
            "models": {},
        }
    else:
        transcript_str = args.transcript or str(stdin_data.get("transcript_path") or "")
        if transcript_str and not Path(os.path.expanduser(transcript_str)).is_file():
            # In cases where the CLI passes a transcript_path that does not exist on disk
            # (e.g. CLI reports product 'antigravity' pointing to ~/.antigravity/antigravity/brain/
            # while transcripts are actually under antigravity-cli/brain/), fall back to discovery.
            transcript_str = ""
        if not transcript_str:
            conv_id = str(
                stdin_data.get("conversation_id")
                or stdin_data.get("conversationId")
                or stdin_data.get("session_id")
                or ""
            ).strip()
            if not conv_id and stdin_data.get("transcript_path"):
                parts = Path(os.path.expanduser(str(stdin_data["transcript_path"]))).parts
                for idx, part in enumerate(parts):
                    if part == "brain" and idx + 1 < len(parts):
                        conv_id = parts[idx + 1]
                        break
            if conv_id:
                search_roots: list[Path] = []
                seen_roots: set[Path] = set()

                def _add_root(p: Path | None) -> None:
                    if p:
                        expanded = p.expanduser().resolve()
                        if expanded not in seen_roots:
                            seen_roots.add(expanded)
                            search_roots.append(expanded)

                if args.gemini_dir:
                    _add_root(Path(args.gemini_dir))
                if cfg.get("gemini_dir"):
                    _add_root(Path(str(cfg["gemini_dir"])))
                _add_root(detect_active_gemini_dir())
                _add_root(Path.home() / ".antigravity")
                _add_root(Path.home() / ".gemini")

                for r in search_roots:
                    candidates = [
                        r / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl",
                    ] + [
                        r / s / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
                        for s in SURFACE_SUBDIRS
                    ]
                    for cand in candidates:
                        if cand.is_file():
                            transcript_str = str(cand)
                            break
                    if transcript_str:
                        break
        model_id = args.model or str((stdin_data.get("model") or {}).get("id") or "")
        rate_card, card_source = load_rate_card(config=cfg, allow_network=False)

        if transcript_str and Path(os.path.expanduser(transcript_str)).is_file():
            result = compute_session_cost(
                Path(os.path.expanduser(transcript_str)),
                rate_card=rate_card,
                default_model=model_id,
            )
            result["source"] = f"transcript:{card_source}"
        else:
            result = {
                "total_usd": 0.0,
                "main_usd": 0.0,
                "subagent_usd": 0.0,
                "subagent_count": 0,
                "cache_savings_usd": 0.0,
                "input_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": 0,
                "steps": 0,
                "estimated": False,
                "source": "empty",
                "models": {},
            }

    if args.write_cache:
        _atomic_write_json(Path(os.path.expanduser(args.write_cache)), result)

    out_format = args.format or ("report" if args.breakdown else "badge")
    if out_format == "json":
        print(json.dumps(result, indent=2))
    elif out_format == "report" or args.breakdown:
        print(format_session_breakdown(result))
    elif out_format == "plain":
        print(format_badge(result, ansi=False))
    else:
        print(format_badge(result, ansi=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
