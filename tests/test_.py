import importlib

import marvin_mcp_server


def test_version() -> None:
    assert (
        importlib.metadata.version("marvin_mcp_server") == marvin_mcp_server.__version__
    )
