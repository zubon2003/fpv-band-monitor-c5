"""5.8 GHz FPV channel table and channel-spec parsing.

Frequencies are the usual analogue FPV assignments in MHz. Band letters follow
the common convention: A (Boscam A / TBS), B (Boscam B), E (Boscam E),
F (Fatshark / Airwave), R (Raceband), L (Lowband).
"""

from __future__ import annotations

from dataclasses import dataclass

BANDS: dict[str, list[int]] = {
    "A": [5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725],
    "B": [5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866],
    "E": [5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945],
    "F": [5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880],
    "R": [5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917],
    "L": [5362, 5399, 5436, 5473, 5510, 5547, 5584, 5621],
}

# The four channels this monitor was built for.
DEFAULT_CHANNELS = "E2,E1,F3,F5"


@dataclass(frozen=True)
class Channel:
    name: str
    freq_mhz: float


def channel_freq(name: str) -> float:
    """'F3' -> 5780.0. Also accepts a bare number ('5780' or '5780.5')."""
    key = name.strip().upper()
    if not key:
        raise ValueError("empty channel name")
    band, index = key[0], key[1:]
    if band in BANDS and index.isdigit():
        n = int(index)
        if 1 <= n <= 8:
            return float(BANDS[band][n - 1])
        raise ValueError(f"channel index out of range (1-8): {name}")
    try:
        return float(key)
    except ValueError:
        raise ValueError(f"unknown channel: {name}") from None


def parse_channels(spec: str) -> list[Channel]:
    """'E2,E1,F3,F5' -> [Channel('E2', 5685.0), ...]

    An entry may also be 'LABEL@FREQ' or a bare frequency in MHz.
    """
    out: list[Channel] = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        if "@" in item:
            label, _, freq = item.partition("@")
            out.append(Channel(label.strip() or freq.strip(), float(freq)))
        else:
            out.append(Channel(item.upper(), channel_freq(item)))
    if not out:
        raise ValueError("no channels given")
    return out


def format_table() -> str:
    lines = ["band " + " ".join(f"{i:>7}" for i in range(1, 9))]
    for band, freqs in BANDS.items():
        lines.append(f"  {band}  " + " ".join(f"{f:>7}" for f in freqs))
    return "\n".join(lines)
