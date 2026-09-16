"""Optional FastAPI adapter for the local application."""

__all__ = ["create_app"]


def __getattr__(name):
    # Read-only result projections also serve Python notebooks without FastAPI.
    if name == "create_app":
        from mobile_sensing.api.app import create_app

        return create_app
    raise AttributeError(name)
