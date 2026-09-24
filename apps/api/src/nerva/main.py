"""ASGI entry point: `uvicorn nerva.main:app`."""

from nerva.app import create_app

app = create_app()
