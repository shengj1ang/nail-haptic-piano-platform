"""Command line entry point: ``python -m melody_generator``."""

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from .config import GeneratorConfig
from .export import export_sequence, summary_text
from .generator import GenerationFailed, generate_sequence
from .layouts import LAYOUTS, describe_layouts
from .theory import KEYS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m melody_generator",
        description=(
            "Generate simple, natural, fixed-fingering practice melodies for "
            "the piano haptic experiment. Writes a .mid, a .json, a .csv and "
            "a .txt summary per sequence."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="hand positions:\n" + describe_layouts(),
    )
    parser.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="how many sequences to generate, using seed, seed+1, ... (default 1)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("melody_out"),
        help="output folder (default ./melody_out)",
    )
    parser.add_argument("--name", help="base filename (default melody_seed<seed>)")

    music = parser.add_argument_group("music")
    music.add_argument(
        "--key", choices=sorted(KEYS), default="c_major", help="default c_major"
    )
    music.add_argument(
        "--layout",
        choices=sorted(LAYOUTS),
        default="middle_c",
        help="hand position (default middle_c)",
    )
    music.add_argument(
        "--notes", type=int, default=15, help="number of note-on events (default 15)"
    )
    music.add_argument(
        "--phrase-plan",
        help="explicit phrase lengths, e.g. 4,4,4,3 (default derived from --notes)",
    )
    music.add_argument("--bpm", type=float, default=60.0, help="default 60")

    articulation = parser.add_argument_group("articulation")
    articulation.add_argument(
        "--gate-mode",
        choices=("fixed_gap", "ratio"),
        default="fixed_gap",
        help="fixed_gap releases every note a constant gap early (default)",
    )
    articulation.add_argument(
        "--release-gap",
        type=float,
        default=0.25,
        help="fixed_gap: beats of silence before the next slot (default 0.25)",
    )
    articulation.add_argument(
        "--gate-ratio",
        type=float,
        default=0.75,
        help="ratio mode: fraction of the slot the key is held (default 0.75)",
    )
    articulation.add_argument(
        "--velocity", type=int, default=80, help="note-on velocity (default 80)"
    )

    rhythm = parser.add_argument_group("rhythm")
    rhythm.add_argument("--min-rests", type=int, default=1)
    rhythm.add_argument("--max-rests", type=int, default=3)
    rhythm.add_argument(
        "--final-beats",
        type=int,
        default=3,
        help="length of the closing note in beats (default 3)",
    )

    hands = parser.add_argument_group("hands")
    hands.add_argument(
        "--min-notes-per-hand",
        type=int,
        default=1,
        help=(
            "on a two-hand position, the fewest notes each hand must play "
            "(default 1; raise it for a more evenly bimanual melody, 0 allows "
            "a one-handed result)"
        ),
    )

    search = parser.add_argument_group("candidate search")
    search.add_argument("--attempts", type=int, default=400)
    search.add_argument("--pool", type=int, default=40)
    search.add_argument("--max-difficulty", type=float, default=0.55)
    search.add_argument("--min-musicality", type=float, default=0.55)

    output = parser.add_argument_group("output")
    output.add_argument(
        "--split-hand-channels",
        action="store_true",
        help="write the left hand on MIDI channel 2 (default: one channel)",
    )
    output.add_argument(
        "--dry-run", action="store_true", help="generate and print, write nothing"
    )
    output.add_argument("--quiet", action="store_true", help="print one line per file")
    output.add_argument(
        "--list-layouts", action="store_true", help="print the hand positions and exit"
    )
    return parser


def config_from_args(args: argparse.Namespace) -> GeneratorConfig:
    base = GeneratorConfig()
    plan = ()
    if args.phrase_plan:
        plan = tuple(int(part) for part in args.phrase_plan.replace(" ", "").split(","))
    return base.with_overrides(
        melody=replace(
            base.melody,
            key=args.key,
            layout=args.layout,
            note_count=args.notes,
            phrase_plan=plan,
        ),
        rhythm=replace(
            base.rhythm,
            min_rests=args.min_rests,
            max_rests=args.max_rests,
            final_note_beats=args.final_beats,
        ),
        timing=replace(
            base.timing,
            bpm=args.bpm,
            gate_mode=args.gate_mode,
            release_gap_beats=args.release_gap,
            gate_ratio=args.gate_ratio,
            base_velocity=args.velocity,
        ),
        validation=replace(
            base.validation,
            min_notes_per_hand=args.min_notes_per_hand,
        ),
        scoring=replace(
            base.scoring,
            candidate_attempts=args.attempts,
            candidate_pool=args.pool,
            max_difficulty=args.max_difficulty,
            min_musicality=args.min_musicality,
        ),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_layouts:
        print(describe_layouts())
        return 0
    if args.count < 1:
        print("--count must be at least 1", file=sys.stderr)
        return 2

    cfg = config_from_args(args)
    failures = 0
    for offset in range(args.count):
        seed = args.seed + offset
        if args.name:
            name = args.name if args.count == 1 else f"{args.name}_{offset + 1:02d}"
        else:
            name = f"melody_seed{seed}"
        try:
            sequence = generate_sequence(seed, cfg=cfg, name=name)
        except (GenerationFailed, ValueError) as error:
            print(f"seed {seed}: {error}", file=sys.stderr)
            failures += 1
            continue

        if args.dry_run:
            print(summary_text(sequence))
            continue

        paths = export_sequence(
            sequence, args.out, split_hand_channels=args.split_hand_channels
        )
        if args.quiet:
            print(paths["midi"])
        else:
            print(summary_text(sequence))
            print("written")
            for kind in ("midi", "json", "csv", "summary"):
                print(f"  {kind:<8s} {paths[kind]}")
            print()
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
