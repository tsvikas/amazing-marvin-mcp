import importlib.metadata

import amazing_marvin_mcp


def test_version() -> None:
    assert (
        importlib.metadata.version("amazing_marvin_mcp")
        == amazing_marvin_mcp.__version__
    )
