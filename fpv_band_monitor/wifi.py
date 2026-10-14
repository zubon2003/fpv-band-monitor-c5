"""Wi-Fi against a group of FPV channels: which Wi-Fi channels can hurt it,
and what a receive-only listen on them (the LCD firmware's FPV WIFI) found.

A Wi-Fi signal on a 20 MHz channel hurts FPV channel k when it overlaps k's
video (Direct), or when a third-order product with one of the group's VTXs
lands on k (IMD): 2 x VTX - Wi-Fi (same width, mirrored about the VTX) or
2 x Wi-Fi - VTX (twice as wide). A wider BSS is a run of 20 MHz channels, so
the check works per 20 MHz channel. The same rules as the LCD's Wi-Fi page.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 20 MHz channels that can reach the FPV bands. Beacons come only on a BSS's
# primary channel, and a 160 MHz BSS on ch 100 ends at 5650 MHz (E4).
CANDIDATES = [100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
              149, 153, 157, 161, 165, 169, 173, 177]
DWELL_MS = 1200          # beacons every ~100 ms
RSSI_STRONG = -50        # full colour from here ...
RSSI_WEAK = -85          # ... faint at and below


def wifi_mhz(ch: int) -> int:
    return 2484 if ch == 14 else (2407 + 5 * ch if ch <= 14 else 5000 + 5 * ch)


@dataclass
class Relation:
    """What a signal on one Wi-Fi channel does to the group's channels."""

    ch: int
    direct: set[int] = field(default_factory=set)        # channel indices
    imd: dict[int, tuple[int, str]] = field(default_factory=dict)  # k -> (vtx, "2V-W" | "2W-V")

    @property
    def any(self) -> bool:
        return bool(self.direct or self.imd)


def relation(freqs: list[float], ch: int, half_mhz: float) -> Relation:
    lo = wifi_mhz(ch) - 10.0
    hi = lo + 20.0
    r = Relation(ch)

    def hits(k: int, a: float, b: float) -> bool:
        return freqs[k] + half_mhz > a and freqs[k] - half_mhz < b

    n = len(freqs)
    for k in range(n):
        if hits(k, lo, hi):
            r.direct.add(k)
    for kind in ("2V-W", "2W-V"):          # 2 x VTX - Wi-Fi first: the stronger one
        for k in range(n):
            if k in r.imd:
                continue
            for v in range(n):
                if v == k:
                    continue
                f = freqs[v]
                a, b = (2 * f - hi, 2 * f - lo) if kind == "2V-W" else (2 * lo - f, 2 * hi - f)
                if hits(k, a, b):
                    r.imd[k] = (v, kind)
                    break
    return r


def channels_to_check(freqs: list[float], half_mhz: float) -> list[Relation]:
    """The candidate Wi-Fi channels that can hurt this group, in order."""
    return [r for r in (relation(freqs, ch, half_mhz) for ch in CANDIDATES) if r.any]


@dataclass
class Ap:
    bssid: str
    ch: int
    lo_mhz: int
    hi_mhz: int
    rssi: int
    ssid: str


@dataclass
class Heard:
    """One listen: frames and the strongest of them; beacon sources."""

    ch: int
    frames: int
    rssi_max: int
    aps: list[Ap]


def parse_listen(head: str, ap_lines: list[str]) -> Heard:
    """'WIFI ch=100 ms=1200 frames=68 ... rssi_max=-50 aps=2' + 'AP ...' lines."""
    kv = dict(w.split("=", 1) for w in head.split()[1:] if "=" in w)
    aps = []
    for line in ap_lines:
        body, _, ssid = line.partition(" ssid=")
        parts = body.split()
        f = dict(w.split("=", 1) for w in parts[2:] if "=" in w)
        lo, _, hi = f.get("mhz", "0-0").partition("-")
        aps.append(Ap(parts[1], int(f.get("ch", 0)), int(lo), int(hi or 0),
                      int(f.get("rssi", -127)), ssid))
    return Heard(int(kv["ch"]), int(kv.get("frames", 0)), int(kv.get("rssi_max", -127)), aps)


def ap_on(aps: list[Ap], ch: int) -> Ap | None:
    """The strongest access point whose signal covers a Wi-Fi channel."""
    f = wifi_mhz(ch)
    best = None
    for a in aps:
        if a.lo_mhz < f < a.hi_mhz and (best is None or a.rssi > best.rssi):
            best = a
    return best


@dataclass
class Verdict:
    kind: str                 # "OK", "Direct", "IMD", "..." (not all checked yet)
    ch: int = 0
    rssi: int = -127
    how: str = ""             # IMD: "2xE2-WiFi" / "2xWiFi-E2"
    ssid: str = ""
    more: int = 0             # further causes heard


def verdicts(names: list[str], checks: list[Relation], heard: dict[int, Heard],
             aps: list[Ap]) -> list[Verdict]:
    """Per FPV channel: the strongest Direct heard, else the strongest IMD."""
    done = all(r.ch in heard for r in checks)
    out = []
    for k in range(len(names)):
        causes = []
        for r in checks:
            h = heard.get(r.ch)
            if h is None or not h.frames:
                continue
            if k in r.direct:
                causes.append((1, h.rssi_max, r, None))
            elif k in r.imd:
                causes.append((0, h.rssi_max, r, r.imd[k]))
        if not causes:
            out.append(Verdict("OK" if done else "..."))
            continue
        causes.sort(key=lambda c: (c[0], c[1]), reverse=True)
        direct, rssi, r, imd = causes[0]
        how = ""
        if imd is not None:
            v, kind = imd
            how = f"2x{names[v]}-WiFi" if kind == "2V-W" else f"2xWiFi-{names[v]}"
        a = ap_on(aps, r.ch)
        out.append(Verdict("Direct" if direct else "IMD", r.ch, rssi, how,
                           (a.ssid or "(hidden)") if a else "", len(causes) - 1))
    return out


def strength(rssi: int, floor: float) -> float:
    """1 at RSSI_STRONG and above, down to floor at RSSI_WEAK and below."""
    a = (rssi - RSSI_WEAK) / (RSSI_STRONG - RSSI_WEAK)
    a = min(1.0, max(0.0, a))
    return floor + (1.0 - floor) * a
