import json
import pytest
from pathlib import Path
from harvey.guidelines import Guideline, GuidelineEngine, DEFAULT_GUIDELINES


def test_guideline_engine_defaults(tmp_path):
    storage = tmp_path / "test_guidelines.json"
    engine = GuidelineEngine(storage_path=storage)
    
    guidelines = engine.list_guidelines()
    assert len(guidelines) == len(DEFAULT_GUIDELINES)
    assert storage.exists()


def test_guideline_matching_discount(tmp_path):
    storage = tmp_path / "test_guidelines.json"
    engine = GuidelineEngine(storage_path=storage)
    
    # Customer asks for discount
    matched = engine.match_guidelines("apakah bisa minta diskon untuk pembelian oven memmert ini?")
    ids = [g.id for g in matched]
    assert "G-04-MARGIN-PROTECTION" in ids


def test_guideline_matching_campus_gov(tmp_path):
    storage = tmp_path / "test_guidelines.json"
    engine = GuidelineEngine(storage_path=storage)
    
    # Campus prospect inquiry
    matched = engine.match_guidelines(
        "kami dari lppm universitas butuh penawaran untuk audit",
        customer_context={"company": "Universitas Padjadjaran"}
    )
    ids = [g.id for g in matched]
    assert "G-03-INSTITUTION-BUDGET" in ids


def test_guideline_matching_sourcing(tmp_path):
    storage = tmp_path / "test_guidelines.json"
    engine = GuidelineEngine(storage_path=storage)
    
    # Sourcing inquiry
    matched = engine.match_guidelines("apakah ada stok ready alat spektrofotometer shimadzu?")
    ids = [g.id for g in matched]
    assert "G-01-STOCK-READY" in ids or "G-02-SOURCING-BY-REQUEST" in ids


def test_format_prompt_block(tmp_path):
    storage = tmp_path / "test_guidelines.json"
    engine = GuidelineEngine(storage_path=storage)
    
    matched = engine.match_guidelines("minta diskon dong pak")
    prompt = engine.format_prompt_block(matched)
    assert "PARLANT BEHAVIORAL GUIDELINES" in prompt
    assert "KONDISI (When)" in prompt
    assert "TINDAKAN (Then)" in prompt

