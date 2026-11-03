"""BLE capture layer (bleak is imported lazily so offline commands work)."""
from __future__ import annotations

import asyncio
from typing import Callable, Dict, List, Optional, Tuple

from .decoders import PLX_CONTINUOUS_MEASUREMENT, PLX_SERVICE


def _require_bleak():
    try:
        from bleak import BleakClient, BleakScanner  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SystemExit(
            "bleak is required for Bluetooth commands: pip install '.[ble]'"
        ) from exc
    from bleak import BleakClient, BleakScanner

    return BleakClient, BleakScanner


async def scan(
    duration: float = 10.0,
    name_filter: Optional[str] = None,
) -> List[dict]:
    """Scan for BLE devices, returns dicts (address, name, rssi, uuids)."""
    _, BleakScanner = _require_bleak()
    results = await BleakScanner.discover(timeout=duration, return_adv=True)
    out = []
    for address, (device, adv) in results.items():
        name = adv.local_name or device.name or ""
        if name_filter and name_filter.lower() not in name.lower():
            continue
        out.append(
            {
                "address": address,
                "name": name,
                "rssi": getattr(adv, "rssi", None),
                "uuids": [str(u) for u in (adv.service_uuids or [])],
            }
        )
    out.sort(key=lambda d: -(d["rssi"] or -999))
    return out


async def watch(
    interval: float = 5.0,
    name_filter: Optional[str] = None,
    lost_rounds: int = 3,
):
    """Continuously scan and yield ("new"|"lost", device_dict) events.

    Emits a "new" event the first time an address is seen, and a "lost"
    event only after `lost_rounds` consecutive rounds without detection,
    so intermittent advertisers do not flap. Power-cycling a device shows
    up as new -> lost -> new on the same address, which identifies it even
    when it advertises without a name.
    """
    _, BleakScanner = _require_bleak()

    detections: asyncio.Queue = asyncio.Queue()
    known: Dict[str, dict] = {}
    misses: Dict[str, int] = {}

    def on_detection(device, adv):
        name = getattr(adv, "local_name", None) or device.name or ""
        if name_filter and name_filter.lower() not in name.lower():
            return
        info = {
            "address": device.address,
            "name": name,
            "rssi": getattr(adv, "rssi", None),
            "uuids": [str(u) for u in (adv.service_uuids or [])],
        }
        misses[device.address] = 0
        if device.address not in known:
            known[device.address] = info
            detections.put_nowait(("new", info))
        else:
            known[device.address] = info

    scanner = BleakScanner(detection_callback=on_detection)
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(interval)
            for address, count in list(misses.items()):
                if count == 0:
                    continue
                if count >= lost_rounds:
                    del misses[address]
                    known.pop(address, None)
                    yield (
                        "lost",
                        {
                            "address": address,
                            "name": "",
                            "rssi": None,
                            "uuids": [],
                        },
                    )
            for address in misses:
                misses[address] += 1
    finally:
        await scanner.stop()


async def dump_gatt(address: str) -> Dict[str, List[dict]]:
    """Connect and dump services/characteristics as a serializable dict."""
    BleakClient, _ = _require_bleak()
    async with BleakClient(address) as client:
        services = client.services
        dump: Dict[str, List[dict]] = {}
        for service in services:
            chars = []
            for ch in service.characteristics:
                chars.append(
                    {
                        "uuid": ch.uuid,
                        "properties": ",".join(sorted(ch.properties)),
                    }
                )
            dump[str(service.uuid)] = chars
        return dump


def is_plx(dump: Dict[str, List[dict]]) -> bool:
    """True if the GATT dump exposes the standard Pulse Oximeter service."""
    service = next((s for s in dump if s.lower().endswith("1822")), None)
    if service is None:
        return False
    return PLX_SERVICE.endswith("1822")


async def find_notify_characteristics(address: str) -> List[Tuple[str, str]]:
    """Return (service_uuid, char_uuid) pairs with notify/indicate."""
    BleakClient, _ = _require_bleak()
    async with BleakClient(address) as client:
        found = []
        for service in client.services:
            for ch in service.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    found.append((str(service.uuid), str(ch.uuid)))
        return found


async def listen(
    address: str,
    service_uuid: Optional[str] = None,
    char_uuid: Optional[str] = None,
    handler: Optional[Callable[[int, bytes], None]] = None,
    plx_notify_uuid: str = PLX_CONTINUOUS_MEASUREMENT,
) -> None:
    """Subscribe to notifications (forever) and forward frames to handler.

    If service/char are not given, the PLX continuous measurement
    characteristic is used when present, otherwise the first notify
    characteristic found.
    """
    BleakClient, _ = _require_bleak()
    async with BleakClient(address) as client:
        if not char_uuid:
            candidates = []
            for s in client.services:
                for ch in s.characteristics:
                    if "notify" in ch.properties or "indicate" in ch.properties:
                        candidates.append((str(s.uuid), str(ch.uuid)))
            if not candidates:
                raise SystemExit("no notify characteristic found on this device")
            plx = [c for c in candidates if c[1].endswith("2a5f")]
            service_uuid, char_uuid = (plx or candidates)[0]
        await client.start_notify(char_uuid, handler or _default_handler)
        await asyncio.Event().wait()


def _default_handler(_sender, data: bytes) -> None:  # pragma: no cover
    print(" ".join(f"{b:02x}" for b in data), flush=True)
