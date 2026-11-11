"""Makes tests/'s pyln-testing fixtures (node_factory, bitcoind, ...) and
helper modules (fixtures, utils, test_liquid_asset_poc) importable from
this directory, the same way tests/pytest.ini's `pythonpath = .` does for
the top-level tests/ dir -- this directory has no such ini of its own
(and doesn't want tests/pytest.ini's, which is scoped to its own dir), so
the path has to be added explicitly here, before any test_*.py in this
directory imports from those modules.
"""
import os
import sys

TESTS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'tests'))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
