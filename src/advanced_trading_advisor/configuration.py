"""Local configuration, without parent search, interpolation or process mutation."""

import os
from pathlib import Path

from dotenv import dotenv_values


def local_environment():
    return {
        **{
            k: v
            for k, v in dotenv_values(Path.cwd() / ".env", interpolate=False).items()
            if v is not None
        },
        **os.environ,
    }
