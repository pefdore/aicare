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


def _wanted(name: str, uuids: List[str], name_filter, service_filter) -> bool:
    if name_filter and name_filter.lower() not in name.lower():
        return False
    if service_filter and not any(
        service_filter.lower() in u.lower() for u in uuids
    ):
        return False
    return True


async def scan(
    duration: float = 10.0,
    name_filter: Optional[str] = None,
    service_filter: Optional[str] = None,
) -> List[dict]:
    """Scan for BLE devices, returns dicts (address, name, rssi, uuids)."""
    _, BleakScanner = _require_bleak()
    results = await BleakScanner.discover(timeout=duration, return_adv=True)
    out = []
    for address, (device, adv) in results.items():
        name = adv.local_name or device.name or ""
        uuids = [str(u) for u in (adv.service_uuids or [])]
        if not _wanted(name, uuids, name_filter, service_filter):
            continue
        out.append(
            {
                "address": address,
                "name": name,
                "rssi": getattr(adv, "rssi", None),
                "uuids": uuids,
            }
        )
    out.sort(key=lambda d: -(d["rssi"] or -999))
    return out


async def watch(
    interval: float = 5.0,
    name_filter: Optional[str] = None,
    lost_rounds: int = 3,
    service_filter: Optional[str] = None,
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
        uuids = [str(u) for u in (adv.service_uuids or [])]
        if not _wanted(name, uuids, name_filter, service_filter):
            return
        info = {
            "address": device.address,
            "name": name,
            "rssi": getattr(adv, "rssi", None),
            "uuids": uuids,
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
            while not detections.empty():
                yield detections.get_nowait()
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


async def find_device(address: str, wait: float = 0.0):
    """Wait for a device to advertise, up to `wait` seconds total."""
    BleakClient, BleakScanner = _require_bleak()
    if wait <= 0:
        return address
    import time

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        device = await BleakScanner.find_device_by_address(address, timeout=5.0)
        if device is not None:
            return device
    raise SystemExit(
        f"device {address} did not advertise within {wait:g}s; "
        "power it on and retry"
    )


async def dump_gatt(address: str, wait: float = 0.0) -> Dict[str, List[dict]]:
    """Connect and dump services/characteristics as a serializable dict."""
    BleakClient, _ = _require_bleak()
    target = await find_device(address, wait)
    async with BleakClient(target) as client:
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
    wait: float = 0.0,
    handler_factory: Optional[Callable[[str], Callable]] = None,
    subscribe_all: bool = False,
) -> None:
    """Subscribe to notifications (forever) and forward frames to handler.

    If service/char are not given, the PLX continuous measurement
    characteristic is used when present, otherwise the first notify
    characteristic found. With subscribe_all=True, every subscribable
    characteristic gets a notification handler built by handler_factory
    (called with the char UUID); failed subscriptions are skipped.
    """
    BleakClient, _ = _require_bleak()
    target = await find_device(address, wait)
    async with BleakClient(target) as client:
        if char_uuid:
            candidates = [(service_uuid or char_uuid, char_uuid)]
        else:
            candidates = []
            for s in client.services:
                for ch in s.characteristics:
                    if "notify" in ch.properties or "indicate" in ch.properties:
                        candidates.append((str(s.uuid), str(ch.uuid)))
            # vendor Aicare chars first (1531/1542/1543), then PLX, rest last
            def rank(c):
                return (
                    0 if c[1].startswith("000015") else 1 if c[1].endswith("2a5f") else 2
                )
            candidates.sort(key=rank)
        if not candidates:
            raise SystemExit("no notify characteristic found on this device")
        subscribed = 0
        last_error: Optional[Exception] = None
        for service, char in candidates:
            if handler_factory is not None:
                char_handler = handler_factory(char)
            else:
                char_handler = handler or _default_handler
            try:
                await client.start_notify(char, char_handler)
            except Exception as exc:  # access denied, not supported, pairing required
                last_error = exc
                print(
                    f"cannot subscribe to {char} ({exc}); trying next...",
                    file=__import__("sys").stderr,
                    flush=True,
                )
                continue
            print(
                f"subscribed to {char} (service {service})",
                file=__import__("sys").stderr,
                flush=True,
            )
            subscribed += 1
            if not subscribe_all:
                await asyncio.Event().wait()
                return
        if subscribed == 0:
            raise SystemExit(
                "could not subscribe to any notify characteristic: "
                f"{last_error}; try pairing the device in Windows Bluetooth settings"
            )
        await asyncio.Event().wait()


async def write(
    address: str,
    char_uuid: str,
    data: bytes,
    handler_factory: Optional[Callable[[str], Callable]] = None,
    listen_after: float = 0.0,
) -> None:
    """Write bytes to a characteristic, optionally listening to all notifies."""
    BleakClient, _ = _require_bleak()
    target = await find_device(address, 0.0)
    async with BleakClient(target) as client:
        if handler_factory is not None and listen_after > 0:
            for s in client.services:
                for ch in s.characteristics:
                    if "notify" in ch.properties or "indicate" in ch.properties:
                        try:
                            await client.start_notify(str(ch.uuid), handler_factory(str(ch.uuid)))
                        except Exception:
                            continue
        try:
            await client.write_gatt_char(char_uuid, data, response=True)
        except Exception:
            await client.write_gatt_char(char_uuid, data, response=False)
        print(
            "wrote " + " ".join(f"{b:02x}" for b in data) + f" to {char_uuid}",
            file=__import__("sys").stderr,
            flush=True,
        )
        if listen_after > 0:
            await asyncio.sleep(listen_after)


DEVICE_INFO_CHARS = {
    "00002a29-0000-1000-8000-00805f9b34fb": "manufacturer",
    "00002a24-0000-1000-8000-00805f9b34fb": "model",
    "00002a25-0000-1000-8000-00805f9b34fb": "serial",
    "00002a27-0000-1000-8000-00805f9b34fb": "hardware_rev",
    "00002a28-0000-1000-8000-00805f9b34fb": "software_rev",
    "00002a26-0000-1000-8000-00805f9b34fb": "firmware_rev",
}


async def device_info(address: str, wait: float = 0.0) -> Dict[str, str]:
    """Connect and read the Device Information service (0x180A)."""
    BleakClient, _ = _require_bleak()
    target = await find_device(address, wait)
    async with BleakClient(target) as client:
        out: Dict[str, str] = {}
        for service in client.services:
            for ch in service.characteristics:
                if str(ch.uuid) in DEVICE_INFO_CHARS and "read" in ch.properties:
                    try:
                        value = bytes(await client.read_gatt_char(ch.uuid))
                        out[DEVICE_INFO_CHARS[str(ch.uuid)]] = value.decode(
                            "utf-8", errors="replace"
                        )
                    except Exception:
                        continue
        return out


async def read(address: str, char_uuid: str) -> bytes:
    """Read a characteristic and return its raw value."""
    BleakClient, _ = _require_bleak()
    target = await find_device(address, 0.0)
    async with BleakClient(target) as client:
        return bytes(await client.read_gatt_char(char_uuid))


def _default_handler(_sender, data: bytes) -> None:  # pragma: no cover
    print(" ".join(f"{b:02x}" for b in data), flush=True)
