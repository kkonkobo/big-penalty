"""`python -m jobhunt.portal.server`: run the portal with uvicorn (one worker). Config comes from the environment."""

from __future__ import annotations

import os
import sys

from .config import Config, ConfigError


def main() -> int:
    os.umask(0o077)
    try:
        config = Config.from_env(os.environ)
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 2
    import uvicorn

    from .app import create_app

    uvicorn.run(create_app(config), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), workers=1, access_log=False,
                server_header=False, proxy_headers=False, limit_concurrency=64, timeout_keep_alive=5, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
