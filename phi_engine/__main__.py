"""``python -m phi_engine ...`` entry point -- delegates to the CLI."""

from __future__ import annotations

from phi_engine.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
