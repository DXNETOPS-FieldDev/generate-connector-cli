"""pytest configuration — adds project root and test/ to sys.path."""

import os
import sys

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_TEST_DIR)

for _p in (_ROOT_DIR, _TEST_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
