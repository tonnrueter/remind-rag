"""The examples in the docstrings (`>>>` lines) are tests too: they run here, so they can't go stale.

Only modules whose examples need no index or REMIND checkout are listed.
"""

from __future__ import annotations

import doctest

import pytest

from remind_rag import chunkers, gams, render, status, store, usage


@pytest.mark.parametrize("module", [gams, usage, chunkers, render, status, store], ids=lambda m: m.__name__)
def test_docstring_examples(module):
    result = doctest.testmod(module, optionflags=doctest.NORMALIZE_WHITESPACE)
    assert result.attempted > 0, f"{module.__name__} has no examples left; drop it from the list"
    assert result.failed == 0, f"{result.failed} example(s) in {module.__name__} failed (details above)"
