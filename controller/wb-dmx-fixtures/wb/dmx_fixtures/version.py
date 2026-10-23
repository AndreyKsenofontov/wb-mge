from importlib.metadata import PackageNotFoundError, version


def get_version() -> str:
    """Version of the installed package; a placeholder when run from the source tree."""
    try:
        return version("wb-dmx-fixtures")
    except PackageNotFoundError:
        return "0.0.0+source"
