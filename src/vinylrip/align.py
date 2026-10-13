# SPDX-License-Identifier: GPL-3.0-or-later
"""Pass 2: choose cuts so that track durations match the identified release."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .split import Plan

WEAK_COST = 0.35
SPEED_RANGE = (0.97, 1.03)
SPEED_STEP = 0.002
DOUBT_MEAN_ERROR = 0.08


@dataclass
class Alignment:
    cuts: list            # chosen internal cut positions (s)
    speed: float          # factor applied to measured durations
    mean_error: float     # mean relative duration error per track
    doubtful: bool


def align(plan: Plan, expected_s: list[float]) -> Alignment | None:
    """Pick len(expected)-1 cuts among the candidates. None if impossible."""
    n = len(expected_s)
    if n < 1:
        return None
    t0, t1 = plan.music_start_s, plan.music_end_s
    if n == 1:
        err = abs((t1 - t0) - expected_s[0]) / expected_s[0]
        return Alignment([], 1.0, err, err > DOUBT_MEAN_ERROR)
    cands = sorted(plan.candidates, key=lambda c: c.pos_s)
    m = len(cands)
    if m < n - 1:
        return None
    pos = np.array([c.pos_s for c in cands])
    ccost = np.array([0.0 if c.strength == "strong" else WEAK_COST for c in cands])
    e = np.array(expected_s, dtype=float)
    best = None
    k = SPEED_RANGE[0]
    while k <= SPEED_RANGE[1] + 1e-9:
        res = _dp(pos, ccost, e, t0, t1, k)
        if res is not None and (best is None or res[0] < best[0]):
            best = (res[0], res[1], k)
        k += SPEED_STEP
    if best is None:
        return None
    total, idx, k = best
    cuts = [float(pos[i]) for i in idx]
    bounds = [t0] + cuts + [t1]
    errs = [abs((bounds[j + 1] - bounds[j]) * k - e[j]) / e[j] for j in range(n)]
    mean_err = float(np.mean(errs))
    return Alignment(cuts, round(k, 4), mean_err, mean_err > DOUBT_MEAN_ERROR)


def _dp(pos, ccost, e, t0, t1, k):
    n, m = len(e), len(pos)
    INF = float("inf")
    # f[j][i]: best cost with j cuts placed, the last one at candidate i
    f = np.full((n, m), INF)
    back = np.full((n, m), -1, dtype=int)
    f[1] = np.abs((pos - t0) * k - e[0]) / e[0] + ccost
    for j in range(2, n):
        for i in range(j - 1, m):
            prev = f[j - 1, :i]
            seg = np.abs((pos[i] - pos[:i]) * k - e[j - 1]) / e[j - 1]
            tot = prev + seg
            a = int(np.argmin(tot))
            if tot[a] < INF:
                f[j, i] = tot[a] + ccost[i]
                back[j, i] = a
    last = np.abs((t1 - pos) * k - e[n - 1]) / e[n - 1]
    final = f[n - 1] + last
    i = int(np.argmin(final))
    if not np.isfinite(final[i]):
        return None
    idx = [i]
    for j in range(n - 1, 1, -1):
        i = int(back[j, i])
        idx.append(i)
    idx.reverse()
    return float(final[idx[-1]]), idx
