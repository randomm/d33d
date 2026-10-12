"""tests/test_issue435_check_keys.py — the server check keys and the UI words agree.

Issue #435: the design loop reports the checks that ran as ``checks_run``
(keys from ``d33d.design_loop.CHECK_KEYS``). The pass card shows a word only for
keys present in ``web/src/copy.ts`` ``passCard.checkWords``. A key missing on
either side is silently dropped from the UI, so the two lists must be equal.
"""

from __future__ import annotations

import re
from pathlib import Path

from d33d.design_loop import CHECK_KEYS

_COPY = Path(__file__).resolve().parents[1] / "web" / "src" / "copy.ts"


def _ui_check_keys() -> tuple[str, ...]:
    text = _COPY.read_text(encoding="utf-8")
    block = re.search(r"checkWords:\s*\{(.*?)\}\s*as Record", text, re.DOTALL)
    assert block is not None, "passCard.checkWords block not found in copy.ts"
    return tuple(re.findall(r"^\s*([a-z_]+):", block.group(1), re.MULTILINE))


def test_ui_check_words_match_server_check_keys_in_order():
    assert _ui_check_keys() == CHECK_KEYS


def test_check_keys_are_unique():
    assert len(set(CHECK_KEYS)) == len(CHECK_KEYS)
