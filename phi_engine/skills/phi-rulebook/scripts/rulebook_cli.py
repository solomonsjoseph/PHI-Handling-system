"""phi-rulebook CLI — resolve / inspect the jurisdiction rulebook (Wave 3 C2).

Metadata-only operator surface over :mod:`scripts.security.phi_rulebook`:

    python -m plugins...phi-rulebook.scripts.rulebook_cli resolve --study Indo-VAP
    python -m ...rulebook_cli show --jurisdictions INDIA,USA

``resolve`` loads the study privacy config, resolves the active rulebook
(comparing to the versioned cache / committed seed, detecting drift) and prints
the provenance — rules SHA-256, cache status, drift flag — never any study data.
``show`` dumps a committed-seed / cache entry's rule metadata for a jurisdiction
set without needing a study.

Exit codes: 0 = resolved (no drift); 3 = resolved but DRIFT detected (the
effective rule set changed since the recorded baseline — operator must confirm);
2 = usage / config error.
"""

from __future__ import annotations

import argparse
import json
import sys

from phi_engine.security.phi_review import load_study_privacy_config
from phi_engine.security.phi_rulebook import (
    cache_filename,
    default_seed_dir,
    read_cache_entry,
    resolve_live_rulebook,
    resolve_rulebook,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DRIFT = 3
EXIT_WEAKENING = 4  # live extraction flagged a protection-weakening rule (review)


def _cmd_resolve(args: argparse.Namespace) -> int:
    try:
        cfg = load_study_privacy_config(args.study)
    except (OSError, ValueError) as exc:
        print(f"error: cannot load privacy config for {args.study!r}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    res = resolve_rulebook(cfg, allow_network=args.allow_network)
    print(
        json.dumps(
            {
                "jurisdictions": list(res.jurisdictions),
                "rules_sha256": res.bundle.rules_sha256,
                "source_mode": res.bundle.source_mode,
                "cache_status": res.cache_status,
                "drift_detected": res.drift_detected,
                "baseline_sha256": res.baseline_sha256,
                "n_rules": len(res.bundle.rules),
            },
            indent=2,
        )
    )
    return EXIT_DRIFT if res.drift_detected else EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    juris = tuple(j.strip().upper() for j in args.jurisdictions.split(",") if j.strip())
    if not juris:
        print("error: --jurisdictions must be a non-empty comma list", file=sys.stderr)
        return EXIT_USAGE
    entry = read_cache_entry(default_seed_dir() / cache_filename(juris), jurisdictions=juris)
    if entry is None:
        print(f"error: no committed seed for {sorted(juris)}", file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(entry, indent=2))
    return EXIT_OK


def _cmd_refresh(args: argparse.Namespace) -> int:
    try:
        cfg = load_study_privacy_config(args.study)
    except (OSError, ValueError) as exc:
        print(f"error: cannot load privacy config for {args.study!r}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    res = resolve_live_rulebook(cfg, allow_network=args.allow_network)
    print(
        json.dumps(
            {
                "jurisdictions": list(res.jurisdictions),
                "rules_sha256": res.bundle.rules_sha256,
                "source_mode": res.bundle.source_mode,
                "cache_status": res.cache_status,
                "drift_detected": res.drift_detected,
                "protection_weakened": res.protection_weakened,
                "offline_warning": res.offline_warning,
                "n_rules": len(res.bundle.rules),
            },
            indent=2,
        )
    )
    if res.protection_weakened:
        return EXIT_WEAKENING
    return EXIT_DRIFT if res.drift_detected else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PHI jurisdiction rulebook tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p_resolve = sub.add_parser("resolve", help="resolve the active rulebook for a study")
    p_resolve.add_argument("--study", required=True, help="study name (config/<study>/)")
    p_resolve.add_argument(
        "--allow-network",
        action="store_true",
        help="permit live official-source freshness probe (default: pinned/offline)",
    )
    p_resolve.set_defaults(func=_cmd_resolve)

    p_show = sub.add_parser("show", help="dump a committed seed rulebook for a jurisdiction set")
    p_show.add_argument("--jurisdictions", required=True, help="comma list, e.g. INDIA,USA")
    p_show.set_defaults(func=_cmd_show)

    p_refresh = sub.add_parser(
        "refresh",
        help="fetch latest official regulations + AI-extract rules (opt-in, N7)",
    )
    p_refresh.add_argument("--study", required=True, help="study name (config/<study>/)")
    p_refresh.add_argument(
        "--allow-network",
        action="store_true",
        help="permit the live official-source fetch (required for AI extraction)",
    )
    p_refresh.set_defaults(func=_cmd_refresh)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
