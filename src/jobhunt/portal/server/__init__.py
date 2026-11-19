"""The portal server (VPS side): see portal/DESIGN.md.

Imports allowed under this package: the standard library, fastapi/starlette, jinja2, pydantic, cryptography, uvicorn
(`__main__` only) and `jobhunt.portal.protocol`. Nothing else from `jobhunt`: the Docker image does not contain it."""
