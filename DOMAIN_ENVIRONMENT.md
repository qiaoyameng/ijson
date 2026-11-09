# Reproducible authoring environment

Upstream: https://github.com/ICRAR/ijson
Upstream commit: 355eea3b15a3b12d6cac0bbff03c70865b9d5218
Verified runtime: 3.13.14 (tags/v3.13.14:fd17997, Jun 10 2026, 13:03:48) [MSC v.1944 64 bit (AMD64)]
Verified platform: win32

Use an isolated virtual environment. Install the pinned dependencies with
`python -m pip install -r requirements-domain.lock`, then `python -m pip install -e .`.
Set the following environment variables before installation and testing:

```text
PYTHONUTF8=1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
IJSON_BUILD_YAJL2C=0
IJSON_BACKEND=python
```

Core baseline:

```text
python -m pytest -q -o addopts= tests/test_basic_parse.py tests/test_items.py tests/test_kvitems.py tests/test_parse.py tests/test_adapters.py
```

Measured pytest process time: 1.137 seconds on the authoring host.
The Python backend is selected; no YAJL compiler or shared library is required.

All documented checks use in-memory fixtures or local temporary files. No production
accounts, data services, credentials or external network endpoints are required after setup.
Reset by making a fresh checkout of this seed and creating a new virtual environment.
Test fixtures remain under their upstream repository license.
