"""Application variables helper."""

import os


def app_var(index: int) -> str:
    """Return application variable by index."""
    # Values are loaded from environment variables for security
    # Map of known app variables
    _vars = {
        0: os.environ.get("MA_APP_VAR_0", ""),
        1: os.environ.get("MA_APP_VAR_1", ""),
        2: os.environ.get("MA_APP_VAR_2", ""),
        3: os.environ.get("MA_APP_VAR_3", ""),
        4: os.environ.get("MA_APP_VAR_4", ""),
        5: os.environ.get("MA_APP_VAR_5", ""),
        6: os.environ.get("MA_APP_VAR_6", ""),
        7: os.environ.get("MA_APP_VAR_7", ""),
        8: os.environ.get("MA_APP_VAR_8", ""),
        9: os.environ.get("MA_APP_VAR_9", ""),
    }
    return _vars.get(index, "")
