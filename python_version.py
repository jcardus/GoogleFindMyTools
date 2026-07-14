import sys


REQUIRED_MAJOR = 3
REQUIRED_MINOR = 12


def require_python_312() -> None:
    version = sys.version_info
    if (version.major, version.minor) == (REQUIRED_MAJOR, REQUIRED_MINOR):
        return

    raise SystemExit(
        "GoogleFindMyTools requires Python 3.12. "
        f"Current interpreter is Python {version.major}.{version.minor}.{version.micro}. "
        "Create a 3.12 venv, then run this command again."
    )
