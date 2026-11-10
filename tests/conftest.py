"""Machine-level config for every test project.

project.yml no longer selects a harness or a model id — it names a model
alias, and the machine's gitchen config resolves it (see
mcp/global_config.py). This fixture *is* that machine: the fake adapter for
dispatch tests, with small/medium/big defined for it.
"""

import yaml
import pytest


@pytest.fixture(autouse=True)
def gitchen_global_config(tmp_path, monkeypatch):
    path = tmp_path / "gitchen-config.yml"
    path.write_text(yaml.safe_dump({
        "harness": "fake",
        "harnesses": {"fake": {"settings": {}, "sandbox": False}},
        "models": {
            "small": {"fake": {"model": "fake-small"}},
            "medium": {"fake": {"model": "fake-medium"}},
            "big": {"fake": {"model": "fake-big"}},
        },
    }))
    monkeypatch.setenv("GITCHEN_CONFIG", str(path))
    return path
