"""Validated container/local entrypoint: python -m gl_api."""

import logging
import sys

import uvicorn

from gl_api.config import ConfigurationError, load_settings


def main() -> int:
    try:
        settings = load_settings()
        settings.database_url()
        settings.database_ssl()
        settings.bot_token()
    except ConfigurationError as error:
        print(f"configuration_error: {error}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(
        "gl_api.app:app",
        host="0.0.0.0",
        port=8000,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=64,
        timeout_graceful_shutdown=10,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
