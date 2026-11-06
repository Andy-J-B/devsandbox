# DevSandbox E2E fixture repository

A tiny dependency-free Python project used by the plan-11 end-to-end suite.
The `src` layout is put on `sys.path` by `tests/conftest.py`, so the project
runs with only the Python standard library plus pytest.
