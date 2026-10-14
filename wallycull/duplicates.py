"""Group duplicates and bursts by perceptual hashes (pHash/dHash) and capture time."""

from __future__ import annotations

from datetime import datetime

import numpy as np


def _bits(hex_hash: str) -> int:
    return int(hex_hash, 16)


def _distances(hashes: np.ndarray, i: int) -> np.ndarray:
    """Hamming distance from hash i to all others (64-bit hashes)."""
    x = np.bitwise_xor(hashes, hashes[i])
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x).astype(np.int32)
    return np.unpackbits(x.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1).astype(np.int32)


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, a: int) -> int:
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def find_groups(images: list[dict], config: dict) -> list[dict]:
    """Returns groups: {"type": "duplicate"|"burst", "members": [image dicts, best first]}."""
    d = config["duplicates"]
    candidates = [b for b in images if b.get("phash") and b.get("dhash")]
    n = len(candidates)
    if n < 2:
        return []
    phash = np.array([_bits(b["phash"]) for b in candidates], dtype=np.uint64)
    dhash = np.array([_bits(b["dhash"]) for b in candidates], dtype=np.uint64)
    times = np.array([datetime.fromisoformat(b["taken_at"]).timestamp() if b.get("taken_at") else np.nan for b in candidates])

    uf = _UnionFind(n)
    is_duplicate = [False] * n
    for i in range(n - 1):
        dp = _distances(phash, i)[i + 1 :]
        dd = _distances(dhash, i)[i + 1 :]
        # Both hashes must match: fewer false hits on similar layouts with different subjects.
        distance = np.maximum(dp, dd)
        time_diff = np.abs(times[i + 1 :] - times[i])
        dup = distance <= d["duplicate_distance"]
        burst = (distance <= d["burst_distance"]) & (time_diff <= d["burst_seconds"])  # NaN times -> False
        for j in np.nonzero(dup | burst)[0]:
            uf.union(i, i + 1 + int(j))
            if dup[j]:
                is_duplicate[i] = is_duplicate[i + 1 + int(j)] = True

    collected: dict[int, list[int]] = {}
    for i in range(n):
        collected.setdefault(uf.find(i), []).append(i)

    groups = []
    for indices in collected.values():
        if len(indices) < 2:
            continue
        members = sorted(
            (candidates[i] for i in indices),
            # At practically equal quality the image with a RAW file wins
            key=lambda b: (
                round(b.get("rank", b["quality"]) or 0, 2) + (0.011 if len(b["files"]) > 1 else 0),
                -((b.get("metrics") or {}).get("edge_width_100") or 9),  # tie: the sharper image
                (b["width"] or 0) * (b["height"] or 0),
            ),
            reverse=True,
        )
        kind = "duplicate" if all(is_duplicate[i] for i in indices) else "burst"
        groups.append({"type": kind, "members": members})
    return groups
