"""Command-line interface: scan, gatt, listen, decode, guess."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import List, Optional

from . import __version__
from . import decoders
from .decoders import (
    FixedOffsets,
    decode_plx,
    guess_offsets,
    heuristic_decode,
)


def parse_frame(text: str) -> bytes:
    """Parse a hex frame in any common notation (0x.., AA:BB, AA BB, AABB)."""
    cleaned = text.replace("0x", "").replace("0X", "")
    cleaned = cleaned.replace(":", " ").replace(",", " ").replace("-", " ")
    cleaned = cleaned.replace(";", " ").replace("_", " ")
    tokens = cleaned.split()
    if not tokens:
        raise ValueError(f"empty frame: {text!r}")
    if any(len(t) > 2 for t in tokens):
        joined = "".join(tokens)
        if len(joined) % 2 != 0:
            raise ValueError(f"odd-length hex frame: {text!r}")
        tokens = [joined[i : i + 2] for i in range(0, len(joined), 2)]
    try:
        return bytes(int(t, 16) for t in tokens)
    except ValueError as exc:
        raise ValueError(f"invalid hex frame: {text!r}") from exc


def cmd_scan(args: argparse.Namespace) -> int:
    from .ble import scan

    devices = asyncio.run(scan(args.duration, args.name_filter))
    for d in devices:
        line = f"{d['address']}  "
        if d["rssi"] is not None:
            line += f"rssi={d['rssi']}  "
        line += d["name"] or "(sans nom)"
        if d["uuids"]:
            line += "  services: " + ", ".join(d["uuids"])
        print(line)
    if not devices:
        print("no device found", file=sys.stderr)
    return 0


def cmd_gatt(args: argparse.Namespace) -> int:
    from .ble import dump_gatt

    dump = asyncio.run(dump_gatt(args.address))
    plx = any(s.startswith("00001822") for s in dump)
    for service, chars in dump.items():
        print(f"service {service}")
        for ch in chars:
            print(f"  char {ch['uuid']}  [{ch['properties']}]")
    if plx:
        print(
            "\nStandard PLX service (0x1822) detected: "
            "try `aicare listen --address "
            f"{args.address} --char {decoders.PLX_CONTINUOUS_MEASUREMENT}`",
            file=sys.stderr,
        )
    else:
        print(
            "\nNo standard PLX service: proprietary protocol. "
            "Use `aicare listen` on a notify characteristic, then `aicare guess`.",
            file=sys.stderr,
        )
    return 0


class ListenPrinter:
    """Formats frames and publishes decoded readings (with stability filter)."""

    def __init__(
        self,
        offsets: Optional[FixedOffsets],
        stable: int,
        as_json: bool,
    ) -> None:
        self.offsets = offsets
        self.stable = stable
        self.as_json = as_json
        self._last: Optional[dict] = None
        self._streak = 0

    def __call__(self, _sender, data: bytes) -> None:
        frame = " ".join(f"{b:02x}" for b in data)
        if not self.as_json:
            print(f"frame: {frame}", flush=True)
        reading: Optional[dict] = None
        if self.offsets is not None:
            if len(data) > max(self.offsets.spo2, self.offsets.pulse):
                reading = self.offsets.decode(data)
        else:
            plx = decode_plx(data)
            if plx is not None:
                reading = plx
            elif not self.as_json:
                for hit in heuristic_decode(data):
                    print(f"  {hit['type']} @ offset {hit['offset']}: {hit['value']}", flush=True)
        if reading is None:
            return
        if self.stable > 1:
            if self._last is not None and all(
                abs(reading[k] - self._last[k]) < 1e-9 for k in reading
            ):
                self._streak += 1
            else:
                self._streak = 1
            self._last = reading
            if self._streak < self.stable:
                return
        if self.as_json:
            print(json.dumps(reading), flush=True)
        else:
            print(
                "  -> " + ", ".join(f"{k}={v:g}" for k, v in reading.items()),
                flush=True,
            )


def cmd_listen(args: argparse.Namespace) -> int:
    from .ble import listen

    offsets = None
    if args.spo2_offset is not None and args.pulse_offset is not None:
        offsets = FixedOffsets(
            spo2=args.spo2_offset,
            pulse=args.pulse_offset,
            pi=args.pi_offset,
        )
    printer = ListenPrinter(offsets=offsets, stable=args.stable, as_json=args.json)
    print(
        f"listening on {args.address} (Ctrl+C to stop)",
        file=sys.stderr,
    )
    try:
        asyncio.run(
            listen(
                args.address,
                service_uuid=args.service,
                char_uuid=args.char,
                handler=printer,
            )
        )
    except KeyboardInterrupt:
        pass
    return 0


def cmd_decode(args: argparse.Namespace) -> int:
    overall = 0
    for frame_text in args.frames:
        data = parse_frame(frame_text)
        print(f"frame: {frame_text} ({len(data)} bytes)")
        plx = decode_plx(data)
        if plx is not None:
            print("  PLX: " + ", ".join(f"{k}={v:g}" for k, v in plx.items()))
            continue
        offsets: Optional[FixedOffsets] = None
        if args.spo2_offset is not None and args.pulse_offset is not None:
            offsets = FixedOffsets(args.spo2_offset, args.pulse_offset, args.pi_offset)
            print(
                "  fixed: "
                + ", ".join(f"{k}={v:g}" for k, v in offsets.decode(data).items())
            )
            continue
        hits = heuristic_decode(data)
        if hits:
            for hit in hits:
                print(
                    f"  {hit['type']} @ offset {hit['offset']}: {hit['value']:g}"
                )
        else:
            print("  no plausible SpO2/pulse byte found")
            overall = 1
    return overall


def cmd_guess(args: argparse.Namespace) -> int:
    frames = [parse_frame(t) for t in args.frames]
    spo2_values = [args.spo2] * len(frames)
    pulse_values = [args.pulse] * len(frames)
    if args.pulse2 is not None:
        if len(frames) < 2:
            raise SystemExit("--pulse2 needs at least two frames")
        pulse_values[-1] = args.pulse2
    try:
        offsets = guess_offsets(frames, spo2_values, pulse_values)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if offsets is None:
        print(
            "no constant offset pair matches these frames; "
            "values may be encoded (scaled, multi-byte or XORed). "
            "Capture more frames with different readings.",
            file=sys.stderr,
        )
        return 1
    print(
        "spo2_offset={} pulse_offset={}".format(offsets.spo2, offsets.pulse)
    )
    print(
        "verify with: aicare listen --address <MAC> "
        f"--spo2-offset {offsets.spo2} --pulse-offset {offsets.pulse}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aicare",
        description="Capture and decode Aicare W013 BLE oximeter frames",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan for BLE devices")
    p_scan.add_argument("--duration", type=float, default=10.0)
    p_scan.add_argument("--name-filter", default=None)
    p_scan.set_defaults(func=cmd_scan)

    p_gatt = sub.add_parser("gatt", help="dump services/characteristics")
    p_gatt.add_argument("--address", required=True)
    p_gatt.set_defaults(func=cmd_gatt)

    p_listen = sub.add_parser("listen", help="subscribe to notifications")
    p_listen.add_argument("--address", required=True)
    p_listen.add_argument("--service", default=None)
    p_listen.add_argument("--char", default=None)
    p_listen.add_argument("--spo2-offset", type=int, default=None)
    p_listen.add_argument("--pulse-offset", type=int, default=None)
    p_listen.add_argument("--pi-offset", type=int, default=None)
    p_listen.add_argument(
        "--stable",
        type=int,
        default=1,
        help="publish only after N identical consecutive frames",
    )
    p_listen.add_argument("--json", action="store_true")
    p_listen.set_defaults(func=cmd_listen)

    p_decode = sub.add_parser("decode", help="decode hex frame(s) offline")
    p_decode.add_argument("frames", nargs="+")
    p_decode.add_argument("--spo2-offset", type=int, default=None)
    p_decode.add_argument("--pulse-offset", type=int, default=None)
    p_decode.add_argument("--pi-offset", type=int, default=None)
    p_decode.set_defaults(func=cmd_decode)

    p_guess = sub.add_parser(
        "guess", help="deduce offsets from frames + on-screen readings"
    )
    p_guess.add_argument("--spo2", type=float, required=True)
    p_guess.add_argument("--pulse", type=float, required=True)
    p_guess.add_argument(
        "--pulse2",
        type=float,
        default=None,
        help="pulse reading of the last frame (if different)",
    )
    p_guess.add_argument("frames", nargs="+")
    p_guess.set_defaults(func=cmd_guess)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - CLI safety net
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
