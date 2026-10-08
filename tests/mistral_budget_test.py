import json
from datetime import date
from pathlib import Path

import pytest

from pdf2epub.mistral_budget import (
    MistralBudgetExceeded,
    allowed_pages,
    reserve_pages,
)


def _config(path: Path, **overrides):
    settings = {
        "enabled": True,
        "monthly_allowance_usd": 10.0,
        "ocr_price_per_1000_pages_usd": 4.0,
        "safety_margin_usd": 0.5,
        "usage_ledger": str(path),
    }
    settings.update(overrides)
    return {"ocr": {"backends": {"mistral": {"free_tier": settings}}}}


def test_free_tier_page_cap_keeps_safety_margin():
    assert allowed_pages(_config(Path("mistral_usage.json"))) == 2375


def test_reserve_pages_writes_a_non_secret_ledger(tmp_path: Path):
    ledger = tmp_path / "mistral_usage.json"
    output_dir = tmp_path / "book"

    result = reserve_pages(
        _config(ledger),
        12,
        source_sha256="source-hash",
        secondary_config_sha256="config-hash",
        output_dir=output_dir,
    )

    assert result["remaining_pages"] == 2363
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    month = date.today().strftime("%Y-%m")
    assert payload["months"][month]["reserved_pages"] == 12
    assert "api_key" not in ledger.read_text(encoding="utf-8")

    status = json.loads((output_dir / "mistral_budget.json").read_text(encoding="utf-8"))
    assert status["status"] == "reserved"
    assert status["remaining_pages"] == 2363


def test_reserve_pages_fails_closed_before_overage(tmp_path: Path):
    ledger = tmp_path / "mistral_usage.json"
    config = _config(ledger)
    reserve_pages(
        config,
        2375,
        source_sha256="first-source",
        secondary_config_sha256="config-hash",
    )

    with pytest.raises(MistralBudgetExceeded, match="free-tier guard blocked"):
        reserve_pages(
            config,
            1,
            source_sha256="second-source",
            secondary_config_sha256="config-hash",
        )

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    month = date.today().strftime("%Y-%m")
    assert payload["months"][month]["reserved_pages"] == 2375


def test_disabled_guard_does_not_create_a_ledger(tmp_path: Path):
    ledger = tmp_path / "mistral_usage.json"
    config = _config(ledger, enabled=False)

    assert reserve_pages(
        config,
        5000,
        source_sha256="source-hash",
        secondary_config_sha256="config-hash",
    ) is None
    assert not ledger.exists()
