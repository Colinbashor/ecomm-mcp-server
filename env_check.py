"""
Show which .env keys are filled in, grouped the way .env.example groups them,
WITHOUT ever printing a value.

Made for setup sessions, especially ones run with an AI assistant: you can
show the output to anyone (or paste it into a chat) because it carries key
names and set/empty/missing only. It reads the files and changes nothing.

Usage:
    python env_check.py                 # every block
    python env_check.py shopify meta    # only blocks whose title contains a word
    python env_check.py --set-only      # hide blocks with nothing filled in

States:
    set      has a value
    empty    present in .env with no value (fine for platforms you skip)
    missing  in .env.example but absent from .env (fine for platforms you skip)
    suspect  the value starts with '#': an inline comment after an empty key
             (`KEY=  # note`) can load as the value and make a script think
             the platform is configured. Put comments on their own line.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
# A block header opens with dashes; its closing dashes may sit on a wrapped
# second line, so they are optional here.
BLOCK = re.compile(r"^#\s*-{3,}\s*(.+?)[\s,]*(?:-{3,})?\s*$")
KEY = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def example_blocks(example: Path) -> list[tuple[str, list[str]]]:
    """[(block title, [keys...])] in .env.example order."""
    blocks: list[tuple[str, list[str]]] = [("(top)", [])]
    for line in example.read_text(encoding="utf-8").splitlines():
        m = BLOCK.match(line)
        if m:
            blocks.append((m.group(1), []))
            continue
        k = KEY.match(line)
        if k:
            blocks[-1][1].append(k.group(1))
    return [(title, keys) for title, keys in blocks if keys]


def state(key: str, values: dict[str, str | None]) -> str:
    if key not in values:
        return "missing"
    v = (values[key] or "").strip()
    if not v:
        return "empty"
    if v.startswith("#"):
        return "suspect"
    return "set"


def report(example: Path, env: Path, words: list[str], set_only: bool) -> list[str]:
    values = dotenv_values(env) if env.exists() else {}
    lines = []
    if not env.exists():
        lines.append(f"No {env.name} yet: copy .env.example to .env and fill in "
                     f"the platforms you use.")
    words = [w.lower() for w in words]
    for title, keys in example_blocks(example):
        if words and not any(w in title.lower() for w in words):
            continue
        states = {k: state(k, values) for k in keys}
        if set_only and not any(s in ("set", "suspect") for s in states.values()):
            continue
        lines.append(f"\n{title}")
        for k in keys:
            lines.append(f"  {states[k]:8s} {k}")
    extra = sorted(set(values) - {k for _, ks in example_blocks(example) for k in ks})
    if extra and not words:
        lines.append("\nIn .env but not in .env.example (typo, or a key you added):")
        lines.extend(f"  {state(k, values):8s} {k}" for k in extra)
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    p.add_argument("words", nargs="*", help="only blocks whose title contains one of these")
    p.add_argument("--set-only", action="store_true", help="hide blocks with nothing filled in")
    p.add_argument("--env", default=str(HERE / ".env"), help=argparse.SUPPRESS)
    p.add_argument("--example", default=str(HERE / ".env.example"), help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    print("\n".join(report(Path(args.example), Path(args.env), args.words, args.set_only)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
