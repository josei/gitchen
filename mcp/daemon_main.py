"""Entrypoint for the detached daemon process started by daemon.start_daemon.
Run as `python3 -m mcp.daemon_main --project-root <path>` from gitchen/.
"""
import argparse

from .daemon import run_forever


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--poll-seconds", type=int, default=20)
    args = ap.parse_args()
    run_forever(args.project_root, args.poll_seconds)


if __name__ == "__main__":
    main()
