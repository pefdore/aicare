"""Decoders for pulse oximeter BLE frames.

Two families are supported:

- PLX standard profile (Bluetooth SIG Pulse Oximeter Service ``0x1822``,
  PLX Continuous Measurement characteristic ``0x2A5F``) using SFLOAT values.
- Proprietary Aicare-style frames: raw bytes at (possibly unknown) offsets,
  with heuristics to guess SpO2 / pulse / perfusion index positions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

PLX_SERVICE = "00001822-0000-1000-8000-00805f9b34fb"
PLX_CONTINUOUS_MEASUREMENT = "00002a5f-0000-1000-8000-00805f9b34fb"

SERVICE_NAMES = {
    "180a": "Device Information",
    "180d": "Heart Rate",
    "180f": "Battery",
    "181a": "Fitness / beacon",
    "181b": "Body Composition (smart scale)",
    "181d": "Weight Scale",
    "1822": "Pulse Oximeter (PLX)",
}


def service_label(uuid: str) -> str:
    """Annotate a service UUID with its standard name when known."""
    short = uuid.split("-")[0].lower()
    if len(short) == 8 and short.startswith("0000"):
        short = short[4:]
    name = SERVICE_NAMES.get(short)
    return f"{uuid} [{name}]" if name else uuid

SPO2_MIN, SPO2_MAX = 50, 100
PULSE_MIN, PULSE_MAX = 30, 250
PI_MIN, PI_MAX = 0.0, 25.5


def decode_sfloat(raw: int) -> float:
    """Decode a BLE medical SFLOAT (IEEE-11073 16-bit).

    12-bit signed mantissa, 4-bit signed exponent (two's complement).
    """
    mantissa = raw & 0x0FFF
    exponent = (raw >> 12) & 0x0F
    if exponent >= 0x08:
        exponent -= 0x10
    if mantissa >= 0x0800:
        mantissa -= 0x1000
    # Reserved special values (NaN, NInf, +Inf, reserved)
    if raw in (0x07FF, 0x0800, 0x07FE, 0x0802, 0x0801):
        return float("nan")
    return mantissa * (10.0**exponent)


def decode_plx(data: bytes) -> Optional[Dict[str, float]]:
    """Decode a PLX Continuous Measurement (0x2A5F) frame.

    Layout: flags (uint16 LE), SpO2 SFLOAT, pulse rate SFLOAT, then optional
    fields driven by the flags. Returns None if the frame is too short.
    """
    if len(data) < 6:
        return None
    flags = data[0] | (data[1] << 8)
    spo2 = decode_sfloat(data[2] | (data[3] << 8))
    pulse = decode_sfloat(data[4] | (data[5] << 8))
    if not (SPO2_MIN <= spo2 <= SPO2_MAX):
        return None
    if not (PULSE_MIN <= pulse <= PULSE_MAX):
        return None
    out: Dict[str, float] = {"spo2": spo2, "pulse": pulse}
    offset = 6
    # bit 0: timestamp present (7 bytes), bit 1: measurement status,
    # bit 2-3: device sport/health status, bit 4: pulse amplitude (2 bytes)
    if flags & 0x0001:
        offset += 7
    if flags & 0x0002:
        offset += 2
    mask = flags & 0x000C
    if mask:
        offset += 2
    if flags & 0x0010:
        offset += 2
    # bit 9: perfusion index follows the optional fields
    if flags & 0x0200 and offset + 2 <= len(data):
        out["pi"] = decode_sfloat(data[offset] | (data[offset + 1] << 8))
    return out


@dataclass
class FixedOffsets:
    """Deterministic decoder for a proprietary frame layout."""

    spo2: int
    pulse: int
    pi: Optional[int] = None
    scale_pi: float = 0.1

    def decode(self, data: bytes) -> Dict[str, float]:
        out: Dict[str, float] = {
            "spo2": float(data[self.spo2]),
            "pulse": float(data[self.pulse]),
        }
        if self.pi is not None and self.pi < len(data):
            out["pi"] = data[self.pi] * self.scale_pi
        return out


def guess_offsets(
    frames: Iterable[bytes],
    spo2_values: Iterable[float],
    pulse_values: Iterable[float],
) -> Optional[FixedOffsets]:
    """Deduce frame offsets from captured frames and on-screen readings.

    ``frames``, ``spo2_values`` and ``pulse_values`` must be aligned (the
    i-th reading corresponds to the i-th frame). Returns a FixedOffsets whose
    position is constant across every frame and matches every reading, or
    None if no consistent offset pair exists.
    """
    frames = list(frames)
    spo2_values = [float(v) for v in spo2_values]
    pulse_values = [float(v) for v in pulse_values]
    if not frames or len(frames) != len(spo2_values) or len(frames) != len(pulse_values):
        raise ValueError("frames, spo2 and pulse lists must be non-empty and aligned")

    def constant_offsets(match) -> List[int]:
        candidates = None
        for frame, value in zip(frames, match):
            here = {i for i in range(len(frame)) if frame[i] == int(value)}
            candidates = here if candidates is None else (candidates & here)
        return sorted(candidates or [])

    spo2_candidates = constant_offsets(spo2_values)
    pulse_candidates = constant_offsets(pulse_values)
    for s in spo2_candidates:
        for p in pulse_candidates:
            if s != p:
                return FixedOffsets(spo2=s, pulse=p)
    return None


def heuristic_decode(data: bytes) -> List[Dict[str, float]]:
    """List plausible SpO2/pulse readings found at any byte offset.

    Used while the real layout is still unknown: bytes whose value looks like
    a valid saturation (50-100) or heart rate (30-250) are reported with
    their position so they can be cross-checked against the device screen.
    """
    hits: List[Dict[str, float]] = []
    for i, value in enumerate(data):
        if SPO2_MIN <= value <= SPO2_MAX:
            hits.append({"offset": i, "type": "spo2?", "value": float(value)})
        if PULSE_MIN <= value <= PULSE_MAX:
            hits.append({"offset": i, "type": "pulse?", "value": float(value)})
    return hits
