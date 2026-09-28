"""Entry point so the webhook sink can run as ``python -m support``."""

from __future__ import annotations

from support.webhook_sink import main

if __name__ == "__main__":
    main()
