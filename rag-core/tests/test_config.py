"""config.py 加载与字段验证。"""
import sys
from pathlib import Path

import pytest

# 从 tests/ 往上找 rag-core 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config


def test_load_config_returns_keys():
    cfg = load_config()
    assert cfg is not None
    assert hasattr(cfg, "keys")
    assert isinstance(cfg.keys, dict)


def test_config_paths_are_absolute():
    cfg = load_config()
    assert cfg.data_root.is_absolute()
    assert cfg.state_db.is_absolute()


def test_config_ragflow_section():
    cfg = load_config()
    rf = cfg.raw.get("ragflow", {})
    assert rf, "ragflow section should exist"
    assert "default_embedding_model" in rf or "timeout_sec" in rf
    # base_url / api_key 在 keys.ini 中，不走 config.toml
