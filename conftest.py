"""
Hub test configuration.

server.py executes DATA_DIR.mkdir() at module level during import. On dev machines,
the repo-root 'data/' is a dead symlink to the prod data path, causing mkdir to raise
FileExistsError. This conftest changes cwd to a fresh temp dir before any test module
is imported, so server.py's startup mkdir calls succeed.
"""
import os
import tempfile

_test_root = tempfile.mkdtemp(prefix="hub_test_")
os.chdir(_test_root)
