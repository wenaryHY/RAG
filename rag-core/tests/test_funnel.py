"""_flash_verdict 的边界用例。"""
import pytest
from audit.funnel import _flash_verdict


@pytest.mark.parametrize("text,expected", [
    ("yes", "yes"),
    ("YES", "yes"),
    ("  yes  ", "yes"),
    ("yes.", "yes"),
    ("yes, but only in context", "yes"),
    ("maybe", "maybe"),
    ("Maybe  ", "maybe"),
    ("no", "no"),
    ("NO", "no"),
    ("  no\n", "no"),
])
def test_flash_verdict_valid(text, expected):
    assert _flash_verdict(text) == expected


@pytest.mark.parametrize("text", [
    "yesterday",
    "yessir",
    "node",
    "nothing",
    "certes",
    "",
    "mo",
    "y",
    "n",
    "ok yes",            # yes 不在开头
])
def test_flash_verdict_fallback_no(text):
    assert _flash_verdict(text) == "no"
