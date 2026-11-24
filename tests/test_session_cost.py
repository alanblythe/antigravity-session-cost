"""Unit and integration tests for antigravity-session-cost."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "skills" / "antigravity-session-cost"
SCRIPTS_DIR = SKILL_DIR / "scripts"
REPO_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import session_cost  # noqa: E402


@pytest.fixture()
def rate_card() -> dict[str, Any]:
    return json.loads((SKILL_DIR / "config" / "rate_card.json").read_text(encoding="utf-8"))


def _write_jsonl(path: Path, records: list[dict[str, Any]], append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")


def _make_brain_transcript(brain_root: Path, conv_id: str, records: list[dict[str, Any]]) -> Path:
    tpath = brain_root / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    _write_jsonl(tpath, records)
    return tpath


def test_single_conversation_pricing(tmp_path: Path, rate_card: dict[str, Any]) -> None:
    """Single conversation accurately prices input, cache_read, and output tokens."""
    tpath = _make_brain_transcript(
        tmp_path / "brain",
        "11111111-1111-1111-1111-111111111111",
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-argon",
                "input_tokens": 100_000,
                "cache_read_tokens": 50_000,
                "output_tokens": 10_000,
            }
        ],
    )

    result = session_cost.compute_session_cost(tpath, rate_card=rate_card, cache_dir=tmp_path / "cache")

    # gemini-4-argon <= 200k: input $2/M, cache_read $0.10/M, output $10/M
    # 0.1M * 2.00 + 0.05M * 0.10 + 0.01M * 10.00 = 0.20 + 0.005 + 0.10 = 0.305
    assert result["total_usd"] == pytest.approx(0.305, abs=1e-6)
    assert result["main_usd"] == pytest.approx(0.305, abs=1e-6)
    assert result["subagent_usd"] == 0.0
    assert result["subagent_count"] == 0
    assert result["estimated"] is False
    assert result["models"]["gemini-4-argon"]["steps"] == 1
    assert session_cost.format_badge(result, ansi=False) == "$0.30" or session_cost.format_badge(result, ansi=False) == "$0.31"


def test_gt_200k_context_tier_switching(rate_card: dict[str, Any]) -> None:
    """Prompt total (input + cache_read) > 200K switches to gt_200k rates."""
    # Exactly 200K -> base tier
    cost_base, _ = session_cost.price_step(
        "gemini-4-argon",
        input_tokens=150_000,
        cache_read_tokens=50_000,
        output_tokens=10_000,
        rate_card=rate_card,
    )
    # 0.15 * 2 + 0.05 * 0.10 + 0.01 * 10 = 0.30 + 0.005 + 0.10 = 0.405
    assert cost_base == pytest.approx(0.405, abs=1e-6)

    # 200,001 -> gt_200k tier ($4/M input, $0.20/M cache_read, $15/M output)
    cost_gt, _ = session_cost.price_step(
        "gemini-4-argon",
        input_tokens=150_001,
        cache_read_tokens=50_000,
        output_tokens=10_000,
        rate_card=rate_card,
    )
    expected_gt = (150_001 * 4.0 + 50_000 * 0.20 + 10_000 * 15.0) / 1_000_000.0
    assert cost_gt == pytest.approx(expected_gt, abs=1e-6)
    assert cost_gt > cost_base


def test_model_switching_and_fallback_estimation(tmp_path: Path, rate_card: dict[str, Any]) -> None:
    """Mid-conversation model switch tracks per-model buckets and flags unknown fallback models."""
    tpath = _make_brain_transcript(
        tmp_path / "brain",
        "22222222-2222-2222-2222-222222222222",
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-3-flash",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
            },
            {
                "type": "PLANNER_RESPONSE",
                "model": "unknown-experimental-model-v9",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
            },
        ],
    )

    result = session_cost.compute_session_cost(tpath, rate_card=rate_card, cache_dir=tmp_path / "cache")

    # gemini-3-flash: 0.1 * 0.50 + 0.01 * 3.00 = 0.05 + 0.03 = 0.08
    # default_fallback: 0.1 * 2.00 + 0.01 * 10.00 = 0.20 + 0.10 = 0.30
    assert result["total_usd"] == pytest.approx(0.38, abs=1e-6)
    assert result["estimated"] is True
    assert set(result["models"].keys()) == {"gemini-3-flash", "unknown-experimental-model-v9"}
    assert result["models"]["unknown-experimental-model-v9"]["estimated"] is True
    assert result["models"]["unknown-experimental-model-v9"]["rate_card_key"] == "default_fallback"
    assert session_cost.format_badge(result, ansi=False) == "~$0.38"


def test_recursive_subagent_and_swarm_rollup_with_cycle_protection(
    tmp_path: Path, rate_card: dict[str, Any]
) -> None:
    """Recursively rolls up local subagent + Swarm workers and prevents infinite cycles."""
    brain = tmp_path / "brain"
    root_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    sub_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    swarm_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    root_path = _make_brain_transcript(
        brain,
        root_id,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-argon",
                "input_tokens": 50_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
                "tool_calls": [
                    {"name": "invoke_subagent", "args": {"conversationId": sub_id}},
                ],
            },
            {
                "type": "TOOL_RESPONSE",
                "content": f"Swarm worker started with conversationId={swarm_id}",
            },
        ],
    )

    # Subagent points back to root_id AND to swarm_id (tests cycle + dedupe)
    _make_brain_transcript(
        brain,
        sub_id,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-3-flash",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
                "tool_calls": [
                    {"name": "send_message", "args": {"recipient": root_id}},
                    {"name": "swarm", "args": {"worker": swarm_id}},
                ],
            }
        ],
    )

    # Swarm worker points back to sub_id (cycle)
    _make_brain_transcript(
        brain,
        swarm_id,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-3-flash",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
                "content": f"Replying to subagent conversation_id {sub_id}",
            }
        ],
    )

    result = session_cost.compute_session_cost(root_path, rate_card=rate_card, cache_dir=tmp_path / "cache")

    # Root: 0.05 * 2 + 0.01 * 10 = 0.20
    # Subagent: 0.1 * 0.5 + 0.01 * 3 = 0.08
    # Swarm worker: 0.1 * 0.5 + 0.01 * 3 = 0.08
    assert result["main_usd"] == pytest.approx(0.20, abs=1e-6)
    assert result["subagent_usd"] == pytest.approx(0.16, abs=1e-6)
    assert result["total_usd"] == pytest.approx(0.36, abs=1e-6)
    assert result["subagent_count"] == 2
    assert session_cost.format_badge(result, ansi=False) == "$0.36 (sub $0.16)"


def test_incremental_byte_offset_caching(tmp_path: Path, rate_card: dict[str, Any]) -> None:
    """Re-reading after appending new lines advances byte offset and avoids double counting."""
    cache_dir = tmp_path / "cache"
    tpath = _make_brain_transcript(
        tmp_path / "brain",
        "33333333-3333-3333-3333-333333333333",
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-3-flash",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
            }
        ],
    )

    first = session_cost.compute_session_cost(tpath, rate_card=rate_card, cache_dir=cache_dir)
    assert first["total_usd"] == pytest.approx(0.08, abs=1e-6)

    cpath = session_cost._transcript_cache_path(tpath, cache_dir)
    cached_meta_1 = json.loads(cpath.read_text(encoding="utf-8"))
    offset_1 = cached_meta_1["offset"]
    assert offset_1 == tpath.stat().st_size

    # Append a second step
    _write_jsonl(
        tpath,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-3-flash",
                "input_tokens": 200_000,
                "cache_read_tokens": 0,
                "output_tokens": 20_000,
            }
        ],
        append=True,
    )

    second = session_cost.compute_session_cost(tpath, rate_card=rate_card, cache_dir=cache_dir)
    # 0.08 + (0.2 * 0.5 + 0.02 * 3 = 0.16) = 0.24
    assert second["total_usd"] == pytest.approx(0.24, abs=1e-6)
    assert second["models"]["gemini-3-flash"]["steps"] == 2

    cached_meta_2 = json.loads(cpath.read_text(encoding="utf-8"))
    assert cached_meta_2["offset"] > offset_1
    assert cached_meta_2["offset"] == tpath.stat().st_size


def test_global_discount_and_per_model_overrides(rate_card: dict[str, Any]) -> None:
    """Global discount scales list prices; per-model overrides replace rates and discount."""
    card = json.loads(json.dumps(rate_card))
    card["global_discount_pct"] = 20.0
    card["overrides"] = {
        "gemini-3-flash": {
            "input": 0.25,
            "output": 1.50,
            "cache_read": 0.02,
            "discount_pct": 0.0,
        }
    }

    # gemini-4-argon gets 20% off list (<=200K: $2 -> $1.60, $10 -> $8.00)
    argon_cost, argon_rates = session_cost.price_step("gemini-4-argon", 100_000, 0, 100_000, card)
    assert argon_rates.input_per_m == pytest.approx(1.60, abs=1e-6)
    assert argon_rates.output_per_m == pytest.approx(8.00, abs=1e-6)
    assert argon_cost == pytest.approx(0.96, abs=1e-6)

    # gemini-4-argon >200K also gets 20% off ($4 -> $3.20, $15 -> $12.00)
    argon_gt_cost, _ = session_cost.price_step("gemini-4-argon", 1_000_000, 0, 1_000_000, card)
    assert argon_gt_cost == pytest.approx(15.20, abs=1e-6)

    # gemini-3-flash uses explicit net override (discount_pct=0.0)
    flash_cost, flash_rates = session_cost.price_step("gemini-3-flash", 1_000_000, 0, 1_000_000, card)
    assert flash_rates.input_per_m == pytest.approx(0.25, abs=1e-6)
    assert flash_rates.output_per_m == pytest.approx(1.50, abs=1e-6)
    assert flash_cost == pytest.approx(1.75, abs=1e-6)


def test_remote_gcs_https_etag_200_304_and_offline_fail_open(tmp_path: Path) -> None:
    """Remote refresh handles 200 OK -> 304 Not Modified -> offline fail-open to cache."""
    cache_dir = tmp_path / "cache"
    remote_card = {
        "schema_version": "1.0",
        "global_discount_pct": 10.0,
        "models": {
            "gemini-4-argon": {"match": ["gemini-4-argon"], "input": 1.0, "output": 5.0, "cache_read": 0.05},
            "default_fallback": {"match": ["*"], "input": 2.0, "output": 10.0, "cache_read": 0.1},
        },
        "overrides": {},
    }

    class _FakeResponse:
        def __init__(self, body: bytes, etag: str) -> None:
            self._buf = io.BytesIO(body)
            self.headers = {"ETag": etag}

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    url = "gs://example-finops-bucket/antigravity/rate_card.json"

    # 1. Initial 200 OK fetch
    with (
        mock.patch.object(session_cost, "_get_adc_access_token", return_value="fake-adc-token"),
        mock.patch.object(
            session_cost.urllib.request,
            "urlopen",
            return_value=_FakeResponse(json.dumps(remote_card).encode("utf-8"), '"etag-v1"'),
        ) as mock_open,
    ):
        ok, msg = session_cost.refresh_remote_rate_card(url, cache_dir=cache_dir, auth_mode="adc")
        assert ok is True
        assert "etag-v1" in msg
        req_sent = mock_open.call_args[0][0]
        assert req_sent.full_url.startswith("https://storage.googleapis.com/storage/v1/b/example-finops-bucket/o/")
        assert req_sent.get_header("Authorization") == "Bearer fake-adc-token"

    # 2. Second fetch sends If-None-Match and handles 304 Not Modified
    err_304 = urllib.error.HTTPError(url, 304, "Not Modified", hdrs=None, fp=None)  # type: ignore[arg-type]
    with (
        mock.patch.object(session_cost, "_get_adc_access_token", return_value="fake-adc-token"),
        mock.patch.object(session_cost.urllib.request, "urlopen", side_effect=err_304) as mock_open,
    ):
        ok, msg = session_cost.refresh_remote_rate_card(url, cache_dir=cache_dir, auth_mode="adc")
        assert ok is True
        assert "304" in msg
        req_sent = mock_open.call_args[0][0]
        assert req_sent.get_header("If-none-match") == '"etag-v1"'

    # 3. Offline network failure fails open to cached remote rate card
    with (
        mock.patch.object(session_cost, "_get_adc_access_token", return_value=None),
        mock.patch.object(
            session_cost.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ),
    ):
        ok, _ = session_cost.refresh_remote_rate_card(url, cache_dir=cache_dir, auth_mode="adc")
        assert ok is False

        cfg = {"rate_card_url": url, "rate_card_ttl_seconds": 0, "auth_mode": "adc"}
        loaded, source = session_cost.load_rate_card(
            config=cfg,
            cache_dir=cache_dir,
            user_config_dir=tmp_path / "empty-config",
            allow_network=True,
        )
        assert source == f"cached:{url}"
        assert loaded["models"]["gemini-4-argon"]["input"] == 1.0


def test_native_cost_passthrough_vs_force_local(tmp_path: Path) -> None:
    """Non-zero stdin cost.total_usd passes through by default; --force-local recalculates."""
    tpath = _make_brain_transcript(
        tmp_path / "brain",
        "44444444-4444-4444-4444-444444444444",
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-3-flash",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
            }
        ],
    )
    payload = json.dumps(
        {
            "transcript_path": str(tpath),
            "model": {"id": "gemini-3-flash"},
            "cost": {"total_usd": 1.25, "subagent_usd": 0.25},
        }
    )

    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }

    # Default: passthrough native cost
    res_native = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "session_cost.py"), "--format", "json"],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    data_native = json.loads(res_native.stdout)
    assert data_native["source"] == "native"
    assert data_native["total_usd"] == pytest.approx(1.25, abs=1e-6)
    assert data_native["main_usd"] == pytest.approx(1.00, abs=1e-6)
    assert data_native["subagent_usd"] == pytest.approx(0.25, abs=1e-6)

    # --force-local: recomputes from transcript ($0.08)
    res_local = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "session_cost.py"), "--format", "json", "--force-local"],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    data_local = json.loads(res_local.stdout)
    assert data_local["source"].startswith("transcript:")
    assert data_local["total_usd"] == pytest.approx(0.08, abs=1e-6)


def test_statusline_wrapper_tees_existing_command(tmp_path: Path) -> None:
    """statusline_wrapper.sh preserves an existing statusLine script and appends cost badge."""
    tpath = _make_brain_transcript(
        tmp_path / "brain",
        "55555555-5555-5555-5555-555555555555",
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-argon",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
            }
        ],
    )

    existing_script = tmp_path / "my_statusline.sh"
    existing_script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 -c 'import json,sys; d=json.load(sys.stdin); print(\"CUSTOM:\" + d[\"model\"][\"id\"])'\n",
        encoding="utf-8",
    )
    existing_script.chmod(0o755)

    payload = json.dumps(
        {
            "transcript_path": str(tpath),
            "model": {"id": "gemini-4-argon"},
            "cost": {"total_usd": 0.0},
        }
    )

    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "AGY_COST_WRAPPED_STATUSLINE": str(existing_script),
    }

    res = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "statusline_wrapper.sh")],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    # gemini-4-argon: 0.1 * 2 + 0.01 * 10 = $0.30
    assert "CUSTOM:gemini-4-argon" in res.stdout
    assert "│" in res.stdout
    assert "$0.30" in res.stdout


def test_historical_report_7d_zero_double_counting(tmp_path: Path, rate_card: dict[str, Any]) -> None:
    """--report 7d scans transcripts flat once each: zero subagent double-counting and accurate session count."""
    brain = tmp_path / "brain"
    now = datetime(2026, 11, 24, 15, 0, 0, tzinfo=timezone.utc)
    today_iso = now.isoformat().replace("+00:00", "Z")
    child_iso = (now + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    old_iso = (now - timedelta(days=12)).isoformat().replace("+00:00", "Z")

    parent_id = "11111111-aaaa-1111-1111-111111111111"
    child_id = "22222222-bbbb-2222-2222-222222222222"
    old_id = "33333333-cccc-3333-3333-333333333333"

    # Parent references child_id via invoke_subagent
    _make_brain_transcript(
        brain,
        parent_id,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-carbon-medium",
                "input_tokens": 100_000,
                "cache_read_tokens": 100_000,
                "output_tokens": 10_000,
                "created_at": today_iso,
                "tool_calls": [
                    {"name": "invoke_subagent", "args": {"conversationId": child_id}},
                ],
            }
        ],
    )
    # Child subagent transcript spawned 30s later (replies back to parent_id)
    _make_brain_transcript(
        brain,
        child_id,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-hydrogen-medium",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
                "created_at": child_iso,
                "tool_calls": [
                    {"name": "send_message", "args": {"recipient": parent_id}},
                ],
            }
        ],
    )
    # Out-of-window transcript (12 days ago -> excluded from 7d)
    _make_brain_transcript(
        brain,
        old_id,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-argon",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
                "created_at": old_iso,
            }
        ],
    )

    report = session_cost.compute_historical_report(
        brain_dirs=[brain],
        rate_card=rate_card,
        window="7d",
        cache_dir=tmp_path / "cache",
        now=now,
    )

    # Parent (gemini-4-carbon <=200K): 0.1*2.00 + 0.1*0.10 + 0.01*10.00 = 0.20 + 0.01 + 0.10 = 0.31
    # Parent cache savings: (2.00 - 0.10) * 0.1 = 0.19
    # Child (gemini-4-hydrogen <=200K): 0.1*1.50 + 0.01*7.50 = 0.15 + 0.075 = 0.225
    # Total = 0.535 (counted once each, NOT 0.31 + 0.225 + 0.225 = 0.76!)
    assert report["total_usd"] == pytest.approx(0.535, abs=1e-6)
    assert report["cache_savings_usd"] == pytest.approx(0.19, abs=1e-6)
    assert report["total_sessions"] == 1
    assert report["subagent_transcripts"] == 1
    assert report["steps"] == 2
    assert set(report["models"].keys()) == {"gemini-4-carbon-medium", "gemini-4-hydrogen-medium"}

    rendered = session_cost.format_report(report)
    assert "### Antigravity Spend Report (7d)" in rendered
    assert "| Date" in rendered
    assert "| Model" in rendered
    assert "saved ~$0.19 via prompt caching" in rendered
    assert "gemini-4-carbon-medium" in rendered
    assert "gemini-4-hydrogen-medium" in rendered


def test_per_model_breakdown_cache_savings_and_gt200k_discount(
    tmp_path: Path, rate_card: dict[str, Any]
) -> None:
    """Per-model breakdown keeps raw model IDs, computes % share, cache hit %, and >200K + discount cache savings."""
    card = json.loads(json.dumps(rate_card))
    card["global_discount_pct"] = 20.0  # 0.8x multiplier

    tpath = _make_brain_transcript(
        tmp_path / "brain",
        "66666666-6666-6666-6666-666666666666",
        [
            {
                # >200K prompt (50K in + 200K cache = 250K) on gemini-4-carbon-medium
                # gt_200k list: input $4.00, cache $0.20, output $15.00
                # with 20% discount: input $3.20, cache $0.16, output $12.00
                # cost: 0.05*3.20 + 0.20*0.16 + 0.01*12.00 = 0.16 + 0.032 + 0.12 = 0.312
                # cache savings: (3.20 - 0.16) * 0.20 = 0.608
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-carbon-medium",
                "input_tokens": 50_000,
                "cache_read_tokens": 200_000,
                "output_tokens": 10_000,
            },
            {
                # <=200K prompt on gemini-4-hydrogen-medium
                # list: input $1.50, cache $0.075, output $7.50
                # with 20% discount: input $1.20, cache $0.06, output $6.00
                # cost: 0.10*1.20 + 0.10*0.06 + 0.01*6.00 = 0.12 + 0.006 + 0.06 = 0.186
                # cache savings: (1.20 - 0.06) * 0.10 = 0.114
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-hydrogen-medium",
                "input_tokens": 100_000,
                "cache_read_tokens": 100_000,
                "output_tokens": 10_000,
            },
        ],
    )

    result = session_cost.compute_session_cost(tpath, rate_card=card, cache_dir=tmp_path / "cache")

    assert result["total_usd"] == pytest.approx(0.498, abs=1e-6)
    assert result["cache_savings_usd"] == pytest.approx(0.722, abs=1e-6)
    assert set(result["models"].keys()) == {"gemini-4-carbon-medium", "gemini-4-hydrogen-medium"}
    assert result["models"]["gemini-4-carbon-medium"]["cost_usd"] == pytest.approx(0.312, abs=1e-6)
    assert result["models"]["gemini-4-carbon-medium"]["cache_savings_usd"] == pytest.approx(0.608, abs=1e-6)
    assert result["models"]["gemini-4-hydrogen-medium"]["cost_usd"] == pytest.approx(0.186, abs=1e-6)
    assert result["models"]["gemini-4-hydrogen-medium"]["cache_savings_usd"] == pytest.approx(0.114, abs=1e-6)

    breakdown = session_cost.format_session_breakdown(result)
    assert "### Session Cost Breakdown" in breakdown
    assert "gemini-4-carbon-medium" in breakdown
    assert "gemini-4-hydrogen-medium" in breakdown
    assert "80.0%" in breakdown  # 200K / 250K cache hit %
    assert "50.0%" in breakdown  # 100K / 200K cache hit %
    assert "62.7%" in breakdown  # 0.312 / 0.498 share
    assert "37.3%" in breakdown  # 0.186 / 0.498 share
    assert "saved ~$0.72 via prompt caching" in breakdown


def test_cli_breakdown_and_report_flags(tmp_path: Path) -> None:
    """CLI --breakdown and --report 7d produce clean Markdown tables."""
    brain = tmp_path / "brain"
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    parent_id = "77777777-7777-7777-7777-777777777777"
    child_id = "88888888-8888-8888-8888-888888888888"

    parent_path = _make_brain_transcript(
        brain,
        parent_id,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-4-carbon-medium",
                "input_tokens": 50_000,
                "cache_read_tokens": 50_000,
                "output_tokens": 10_000,
                "created_at": now_iso,
                "tool_calls": [
                    {"name": "invoke_subagent", "args": {"conversationId": child_id}},
                ],
            }
        ],
    )
    _make_brain_transcript(
        brain,
        child_id,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-3-flash",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
                "created_at": now_iso,
            }
        ],
    )

    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }

    res_bd = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "session_cost.py"), "--transcript", str(parent_path), "--breakdown"],
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    assert "Session Cost Breakdown" in res_bd.stdout
    assert "Subagents:" in res_bd.stdout
    assert "gemini-4-carbon-medium" in res_bd.stdout
    assert "gemini-3-flash" in res_bd.stdout

    res_rep = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "session_cost.py"),
            "--report",
            "7d",
            "--brain-dir",
            str(brain),
            "--format",
            "json",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    rep_data = json.loads(res_rep.stdout)
    assert rep_data["total_sessions"] == 1
    assert rep_data["subagent_transcripts"] == 1
    # Parent: 0.05*2 + 0.05*0.1 + 0.01*10 = 0.205; Child: 0.1*0.5 + 0.01*3 = 0.08 -> 0.285
    assert rep_data["total_usd"] == pytest.approx(0.285, abs=1e-6)


def test_badge_ansi_color_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """format_badge applies green (< warn), yellow (warn..high), and red (>= high) ANSI colors."""
    monkeypatch.delenv("AGY_COST_WARN_USD", raising=False)
    monkeypatch.delenv("AGY_COST_HIGH_USD", raising=False)

    low = session_cost.format_badge({"total_usd": 0.42}, ansi=True)
    med = session_cost.format_badge({"total_usd": 1.00}, ansi=True)
    med2 = session_cost.format_badge({"total_usd": 4.99}, ansi=True)
    high = session_cost.format_badge({"total_usd": 5.00}, ansi=True)

    assert low.startswith("\033[92m") and low.endswith("\033[0m")
    assert med.startswith("\033[93m") and med.endswith("\033[0m")
    assert med2.startswith("\033[93m") and med2.endswith("\033[0m")
    assert high.startswith("\033[91m") and high.endswith("\033[0m")

    # Custom env thresholds
    monkeypatch.setenv("AGY_COST_WARN_USD", "0.25")
    monkeypatch.setenv("AGY_COST_HIGH_USD", "0.50")
    assert session_cost.format_badge({"total_usd": 0.10}, ansi=True).startswith("\033[92m")
    assert session_cost.format_badge({"total_usd": 0.30}, ansi=True).startswith("\033[93m")
    assert session_cost.format_badge({"total_usd": 0.60}, ansi=True).startswith("\033[91m")


def test_cache_v1_invalidation_reparse(tmp_path: Path, rate_card: dict[str, Any]) -> None:
    """Legacy cache files without cache_version: 2 are cleanly re-parsed with created_at timestamps."""
    cache_dir = tmp_path / "cache"
    ts = "2026-11-24T12:00:00Z"
    tpath = _make_brain_transcript(
        tmp_path / "brain",
        "99999999-9999-9999-9999-999999999999",
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-3-flash",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
                "created_at": ts,
            }
        ],
    )
    stat = tpath.stat()
    cpath = session_cost._transcript_cache_path(tpath, cache_dir)
    session_cost._atomic_write_json(
        cpath,
        {
            "path": str(tpath.resolve()),
            "offset": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "child_ids": [],
            "steps": [["gemini-3-flash", 100_000, 0, 10_000]],
        },
    )

    delta = session_cost._read_transcript_incremental(tpath, default_model="", cache_dir=cache_dir)
    assert len(delta.steps) == 1
    assert delta.steps[0][4] == ts
    updated = json.loads(cpath.read_text(encoding="utf-8"))
    assert updated["cache_version"] == 2


def test_detect_active_gemini_dir_linux_proc_and_ps_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """detect_active_gemini_dir inspects ancestor /proc and ps fallback deterministically."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # 1. Linux /proc parser handles comm with spaces/parens and NUL-separated cmdline
    proc_root = tmp_path / "proc"
    (proc_root / "200").mkdir(parents=True)
    (proc_root / "200" / "stat").write_text(
        "200 (python3 (sub)) S 100 200 200 0 -1 0 0 0 0 0\n",
        encoding="utf-8",
    )
    (proc_root / "200" / "cmdline").write_bytes(b"python3\x00script.py\x00")

    (proc_root / "100").mkdir(parents=True)
    (proc_root / "100" / "stat").write_text(
        "100 (agy) S 1 100 100 0 -1 0 0 0 0 0\n",
        encoding="utf-8",
    )
    custom_dir = tmp_path / "custom-proc-agy"
    (proc_root / "100" / "cmdline").write_bytes(
        f"/usr/local/bin/agy\x00--gemini_dir\x00{custom_dir}\x00".encode("utf-8")
    )

    with mock.patch.object(
        session_cost,
        "_read_process_info",
        side_effect=lambda pid: session_cost._read_linux_proc(pid, proc_root=proc_root),
    ):
        assert session_cost.detect_active_gemini_dir(start_pid=200) == custom_dir.resolve()

    # 2. language_server_* with --config_dir=<path>/config resolves parent
    ls_root = tmp_path / "ls-custom"
    proc_table: dict[int, tuple[int | None, list[str]]] = {
        300: (250, ["bash", "-lc", "session_cost.py"]),
        250: (
            1,
            [
                "/opt/agy/bin/language_server_linux_x64",
                f"--config_dir={ls_root / 'config'}",
            ],
        ),
        1: (0, ["/sbin/init"]),
    }
    with mock.patch.object(
        session_cost, "_read_process_info", side_effect=lambda pid: proc_table.get(pid)
    ):
        assert session_cost.detect_active_gemini_dir(start_pid=300) == ls_root.resolve()

    # 3. ps fallback parses `ppid args` cleanly when /proc is absent
    ps_outputs = {
        50: "  40 /bin/zsh\n",
        40: "   1 /usr/local/bin/agy --gemini-dir=~/ps-agy\n",
    }

    def _fake_ps(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        pid_arg = int(cmd[-1])
        out = ps_outputs.get(pid_arg, "")
        return subprocess.CompletedProcess(cmd, 0 if out else 1, stdout=out, stderr="")

    with mock.patch.object(session_cost.subprocess, "run", side_effect=_fake_ps):
        info = session_cost._read_ps_proc(40)
        assert info == (1, ["/usr/local/bin/agy", "--gemini-dir=~/ps-agy"])

    with mock.patch.object(
        session_cost, "_read_process_info", side_effect=session_cost._read_ps_proc
    ):
        with mock.patch.object(session_cost.subprocess, "run", side_effect=_fake_ps):
            assert (
                session_cost.detect_active_gemini_dir(start_pid=50)
                == (home / "ps-agy").resolve()
            )

    # 4. Standalone terminal chain (no agy/language_server ancestor) returns None
    standalone_table: dict[int, tuple[int | None, list[str]]] = {
        90: (80, ["bash"]),
        80: (1, ["tmux"]),
        1: (0, ["/sbin/init"]),
    }
    with mock.patch.object(
        session_cost,
        "_read_process_info",
        side_effect=lambda pid: standalone_table.get(pid),
    ):
        assert session_cost.detect_active_gemini_dir(start_pid=90) is None


def test_resolve_brain_dirs_active_agy_with_custom_gemini_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ancestor `agy --gemini_dir /tmp/custom-agy` resolves ONLY that brain even when ~/.gemini exists."""
    home = tmp_path / "home"
    xdg_cfg = tmp_path / "xdg_config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_cfg))
    monkeypatch.delenv("AGY_COST_GEMINI_DIR", raising=False)
    monkeypatch.delenv("GEMINI_DIR", raising=False)

    (home / ".gemini" / "antigravity-cli" / "brain").mkdir(parents=True)
    (home / ".antigravity" / "antigravity-cli" / "brain").mkdir(parents=True)

    custom_agy = tmp_path / "custom-agy"
    custom_cli_brain = custom_agy / "antigravity-cli" / "brain"
    custom_app_brain = custom_agy / "antigravity" / "brain"
    custom_cli_brain.mkdir(parents=True)
    custom_app_brain.mkdir(parents=True)

    proc_table: dict[int, tuple[int | None, list[str]]] = {
        20: (10, ["bash"]),
        10: (1, ["/usr/local/bin/agy", "--gemini_dir", str(custom_agy)]),
    }
    monkeypatch.setattr(session_cost.os, "getppid", lambda: 20)
    monkeypatch.setattr(session_cost, "_read_process_info", lambda pid: proc_table.get(pid))

    dirs = session_cost.resolve_brain_dirs()
    assert [p.resolve() for p in dirs] == [custom_cli_brain.resolve()]

    # --all-surfaces scans surfaces strictly inside the single active root
    all_dirs = session_cost.resolve_brain_dirs(all_surfaces=True)
    assert [p.resolve() for p in all_dirs] == [
        custom_cli_brain.resolve(),
        custom_app_brain.resolve(),
    ]


def test_resolve_brain_dirs_active_agy_without_custom_flag_ignores_antigravity_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ancestor `agy` with no --gemini_dir resolves ONLY ~/.gemini even when ~/.antigravity and config.json exist."""
    home = tmp_path / "home"
    xdg_cfg = tmp_path / "xdg_config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_cfg))
    monkeypatch.delenv("AGY_COST_GEMINI_DIR", raising=False)
    monkeypatch.delenv("GEMINI_DIR", raising=False)

    gemini_cli_brain = home / ".gemini" / "antigravity-cli" / "brain"
    gemini_app_brain = home / ".gemini" / "antigravity" / "brain"
    antigravity_cli_brain = home / ".antigravity" / "antigravity-cli" / "brain"
    for d in (gemini_cli_brain, gemini_app_brain, antigravity_cli_brain):
        d.mkdir(parents=True)

    cfg_dir = xdg_cfg / "antigravity-session-cost"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"gemini_dir": str(home / ".antigravity")}),
        encoding="utf-8",
    )

    proc_table: dict[int, tuple[int | None, list[str]]] = {
        20: (10, ["bash"]),
        10: (1, ["agy"]),
    }
    monkeypatch.setattr(session_cost.os, "getppid", lambda: 20)
    monkeypatch.setattr(session_cost, "_read_process_info", lambda pid: proc_table.get(pid))

    dirs = session_cost.resolve_brain_dirs()
    assert [p.resolve() for p in dirs] == [gemini_cli_brain.resolve()]

    all_dirs = session_cost.resolve_brain_dirs(all_surfaces=True)
    assert [p.resolve() for p in all_dirs] == [
        gemini_cli_brain.resolve(),
        gemini_app_brain.resolve(),
    ]


def test_resolve_brain_dirs_standalone_shell_and_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standalone shell falls back to config.json gemini_dir, then ~/.gemini (never merging both), and respects 6-tier precedence."""
    home = tmp_path / "home"
    xdg_cfg = tmp_path / "xdg_config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_cfg))
    monkeypatch.delenv("AGY_COST_GEMINI_DIR", raising=False)
    monkeypatch.delenv("GEMINI_DIR", raising=False)

    gemini_cli_brain = home / ".gemini" / "antigravity-cli" / "brain"
    antigravity_cli_brain = home / ".antigravity" / "antigravity-cli" / "brain"
    gemini_cli_brain.mkdir(parents=True)
    antigravity_cli_brain.mkdir(parents=True)

    # Simulate standalone terminal (no agy / language_server ancestor)
    monkeypatch.setattr(session_cost, "detect_active_gemini_dir", lambda: None)

    # Tier 6: No config.json -> ONLY ~/.gemini/antigravity-cli/brain (never merges ~/.antigravity)
    assert [p.resolve() for p in session_cost.resolve_brain_dirs()] == [
        gemini_cli_brain.resolve()
    ]

    # Tier 5: Configured gemini_dir in config.json
    custom_cfg_root = tmp_path / "custom_from_config"
    (custom_cfg_root / "antigravity-cli" / "brain").mkdir(parents=True)
    (custom_cfg_root / "antigravity" / "brain").mkdir(parents=True)

    cfg_dir = xdg_cfg / "antigravity-session-cost"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"gemini_dir": str(custom_cfg_root)}),
        encoding="utf-8",
    )

    assert session_cost.load_runtime_config()["gemini_dir"] == str(custom_cfg_root)
    assert session_cost.resolve_brain_dirs() == [custom_cfg_root / "antigravity-cli" / "brain"]
    assert session_cost.resolve_brain_dirs(all_surfaces=True) == [
        custom_cfg_root / "antigravity-cli" / "brain",
        custom_cfg_root / "antigravity" / "brain",
    ]

    # Tier 4: Active agy ancestor overrides config.json
    active_root = tmp_path / "active_from_agy"
    monkeypatch.setattr(session_cost, "detect_active_gemini_dir", lambda: active_root)
    assert session_cost.resolve_brain_dirs() == [active_root / "antigravity-cli" / "brain"]

    # Tier 3: GEMINI_DIR / AGY_COST_GEMINI_DIR / --gemini-dir override Tier 4 & Tier 5
    gemini_env_root = tmp_path / "custom_from_gemini_env"
    monkeypatch.setenv("GEMINI_DIR", str(gemini_env_root))
    assert session_cost.resolve_brain_dirs() == [gemini_env_root / "antigravity-cli" / "brain"]

    env_root = tmp_path / "custom_from_env"
    monkeypatch.setenv("AGY_COST_GEMINI_DIR", str(env_root))
    assert session_cost.resolve_brain_dirs() == [env_root / "antigravity-cli" / "brain"]

    cli_root = tmp_path / "custom_from_cli"
    assert session_cost.resolve_brain_dirs(gemini_dir=str(cli_root)) == [
        cli_root / "antigravity-cli" / "brain"
    ]

    # Tier 2: --transcript parent brain/ overrides --gemini-dir and env (when not --all-surfaces)
    t_brain = tmp_path / "transcript_root" / "brain"
    tpath = _make_brain_transcript(t_brain, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", [])
    assert session_cost.resolve_brain_dirs(
        gemini_dir=str(cli_root), transcript_path=str(tpath)
    ) == [t_brain.resolve()]

    # Tier 1: --brain-dir overrides everything
    explicit_brain = tmp_path / "explicit_brain"
    assert session_cost.resolve_brain_dirs(
        brain_dir=str(explicit_brain),
        gemini_dir=str(cli_root),
        transcript_path=str(tpath),
    ) == [explicit_brain]


def test_install_sh_persists_gemini_dir_without_clobbering_settings(tmp_path: Path) -> None:
    """install.sh --gemini-dir ... --mode skill-only persists gemini_dir in config.json."""
    home = tmp_path / "home"
    xdg_cfg = tmp_path / "xdg_config"
    custom_gemini = home / ".antigravity"
    custom_gemini.mkdir(parents=True)

    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg_cfg),
    }
    subprocess.run(
        [
            "bash",
            str(REPO_SCRIPTS_DIR / "install.sh"),
            "--gemini-dir",
            str(custom_gemini),
            "--mode",
            "skill-only",
            "--non-interactive",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    cfg = json.loads((xdg_cfg / "antigravity-session-cost" / "config.json").read_text(encoding="utf-8"))
    assert cfg["gemini_dir"] == str(custom_gemini.resolve())
    assert (custom_gemini / "config" / "skills" / "antigravity-session-cost").is_symlink()
    assert not (custom_gemini / "antigravity-cli" / "settings.json").exists()


def test_plugin_manifest_and_self_contained_skill_bundle() -> None:
    """plugin.json is valid and skills/antigravity-session-cost/ is 100% self-contained."""
    manifest_path = REPO_ROOT / "plugin.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "antigravity-session-cost"
    assert manifest["version"] == "1.0.0"
    assert manifest["description"]

    expected_files = [
        SKILL_DIR / "SKILL.md",
        SKILL_DIR / "config" / "rate_card.json",
        SKILL_DIR / "scripts" / "session_cost.py",
        SKILL_DIR / "scripts" / "statusline_wrapper.sh",
        SKILL_DIR / "scripts" / "statusline_standalone.sh",
        SKILL_DIR / "scripts" / "stop_hook.sh",
    ]
    for path in expected_files:
        assert path.is_file(), f"Missing self-contained skill file: {path}"

    assert not (REPO_ROOT / "config").exists()
    assert session_cost.SKILL_ROOT == SKILL_DIR.resolve()
    assert session_cost.BUNDLED_RATE_CARD == (SKILL_DIR / "config" / "rate_card.json").resolve()


def test_nonexistent_stdin_transcript_path_falls_back_to_discovery(tmp_path: Path) -> None:
    """When stdin passes a non-existent transcript_path (e.g. mismatched product surface), fall back to conv_id discovery."""
    conv_id = "55555555-5555-5555-5555-555555555555"
    actual_tpath = _make_brain_transcript(
        tmp_path / "antigravity-cli" / "brain",
        conv_id,
        [
            {
                "type": "PLANNER_RESPONSE",
                "model": "gemini-3-flash",
                "input_tokens": 100_000,
                "cache_read_tokens": 0,
                "output_tokens": 10_000,
            }
        ],
    )
    nonexistent_tpath = tmp_path / "antigravity" / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    assert actual_tpath.is_file()
    assert not nonexistent_tpath.exists()

    payload = json.dumps(
        {
            "conversation_id": conv_id,
            "transcript_path": str(nonexistent_tpath),
            "model": {"id": "gemini-3-flash"},
        }
    )

    env = {
        **os.environ,
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }

    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "session_cost.py"),
            "--gemini-dir",
            str(tmp_path),
            "--format",
            "plain",
        ],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    assert res.stdout.strip() == "$0.08"

