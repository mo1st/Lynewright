"""Reusable pytest support for Lynewright integration packages."""

__all__ = ["LynewrightHarness"]


def __getattr__(name: str):
    if name == "LynewrightHarness":
        from .harness import LynewrightHarness

        return LynewrightHarness
    raise AttributeError(name)
