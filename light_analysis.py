"""
Light analysis — permutation tests comparing payload vs panel light,
and payload ON vs OFF light values.

Data source: mission_data.db (packets.decoded_json)

Test 1 (paired): Is payload light significantly different from panel light?
Test 2 (block):  Is payload light significantly different when ON vs OFF?
"""

import json
import sqlite3
import sys
from pathlib import Path
from typing import Literal

import matplotlib.pyplot as plt
import numpy as np


## Change this to be where the db file you are working on is located.
## Please do not run this on the pi, we can download the data.
DB_PATH = Path(__file__).parent / "mission_data.db"


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (timestamps, payl_light, pan_faces, pay_set_labels).

    pan_faces has shape (1, n_timesteps) — the single FSM_pan_light reading
    reshaped so side_panel() accepts it unchanged.
    pay_set_labels is a bool array: True = payload ON.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT received_at, decoded_json FROM packets "
        "WHERE decoded_json IS NOT NULL ORDER BY received_at"
    )
    rows = cur.fetchall()
    conn.close()

    timestamps, payl, pan, labels = [], [], [], []
    for row in rows:
        try:
            decoded = json.loads(row["decoded_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if "FSM_payl_light" not in decoded or "FSM_pan_light" not in decoded:
            continue
        timestamps.append(row["received_at"])
        payl.append(float(decoded["FSM_payl_light"]))
        pan.append(float(decoded["FSM_pan_light"]))
        pset = decoded.get("FSM_pay_set", False)
        if isinstance(pset, str):
            labels.append(pset.lower() in ("1", "true", "on", "yes"))
        else:
            labels.append(bool(pset))

    if not payl:
        sys.exit("No light data found in the database.")

    return (
        np.array(timestamps),
        np.array(payl, dtype=float),
        np.array(pan, dtype=float).reshape(1, -1),
        np.array(labels, dtype=bool),
    )


# ── Test statistics ───────────────────────────────────────────────────────────

def mean_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(a) - np.mean(b))

def median_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.median(a) - np.median(b))

def ratio_of_means(a: np.ndarray, b: np.ndarray) -> float:
    mnb = np.mean(b)
    if mnb == 0:
        return np.inf if np.mean(a) > 0 else 1.0
    return float(np.mean(a) / mnb)

def ratio_of_medians(a: np.ndarray, b: np.ndarray) -> float:
    mdb = np.median(b)
    if mdb == 0:
        return np.inf if np.median(a) > 0 else 1.0
    return float(np.median(a) / mdb)

_TEST_STATS = {
    "mean_diff":       mean_diff,
    "median_diff":     median_diff,
    "ratio_of_means":  ratio_of_means,
    "ratio_of_medians": ratio_of_medians,
}


# ── Side-panel aggregation ────────────────────────────────────────────────────

def side_panel(
    pan_faces: np.ndarray,
    method: Literal["max", "mean"],
) -> np.ndarray:
    """Reduce (n_faces, n_timesteps) → (n_timesteps,)."""
    if not all(len(arr) == len(pan_faces[0]) for arr in pan_faces):
        raise ValueError("pan_faces contains differently shaped arrays.")
    if method == "max":
        return pan_faces.max(axis=0)
    if method == "mean":
        return pan_faces.mean(axis=0)
    raise ValueError(f"Unknown method: {method}")


# ── Block helpers (for Test 2) ────────────────────────────────────────────────

def find_blocks(labels: np.ndarray) -> list[tuple[int, int, bool]]:
    blocks, i, n = [], 0, len(labels)
    while i < n:
        j = i
        while j < n and labels[j] == labels[i]:
            j += 1
        blocks.append((i, j, bool(labels[i])))
        i = j
    return blocks

def block_labels(labels: np.ndarray) -> tuple[list, np.ndarray]:
    blocks = find_blocks(labels)
    slices = [(s, e) for s, e, _ in blocks]
    blabels = np.array([lab for _, _, lab in blocks])
    return slices, blabels


# ── Permutation tests ─────────────────────────────────────────────────────────

def paired_ptest(
    a: np.ndarray,
    b: np.ndarray,
    statistic: str,
    n_permutations: int = 10_000,
    seed: int = 42,
) -> dict:
    rng = np.random.default_rng(seed)
    fn = _TEST_STATS[statistic]
    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.shape != b.shape:
        raise ValueError("Paired test requires equal-length arrays.")
    ts_obs = fn(a, b)
    n = len(a)
    flips = rng.choice([0, 1], size=(n_permutations, n)).astype(bool)
    ts_perm = np.array([fn(np.where(f, b, a), np.where(f, a, b)) for f in flips])
    p = (np.sum(ts_perm >= ts_obs) + 1) / (n_permutations + 1)
    return {
        "test": f"paired_{statistic}",
        "observed": ts_obs,
        "p_value": p,
        "null_distribution": ts_perm,
        "n_permutations": n_permutations,
    }


def block_ptest(
    values: np.ndarray,
    labels: np.ndarray,
    statistic: str,
    n_permutations: int = 10_000,
    seed: int = 42,
) -> dict:
    rng = np.random.default_rng(seed)
    fn = _TEST_STATS[statistic]
    values = np.asarray(values, float)
    labels = np.asarray(labels, bool)
    ts_obs = fn(values[labels], values[~labels])
    slices, blabels = block_labels(labels)
    n_blocks = len(slices)
    n_on = int(blabels.sum())
    ts_perm = np.empty(n_permutations)
    for i in range(n_permutations):
        perm = np.zeros(n_blocks, dtype=bool)
        perm[rng.choice(n_blocks, size=n_on, replace=False)] = True
        mask = np.zeros(len(values), dtype=bool)
        for (s, e), lab in zip(slices, perm):
            mask[s:e] = lab
        ts_perm[i] = fn(values[mask], values[~mask])
    p = (np.sum(ts_perm >= ts_obs) + 1) / (n_permutations + 1)
    return {
        "test": f"block_{statistic}",
        "observed": ts_obs,
        "p_value": p,
        "null_distribution": ts_perm,
        "n_permutations": n_permutations,
        "n_blocks": n_blocks,
        "n_on_blocks": n_on,
    }


# ── Plotting ──────────────────────────────────────────────────────────────────

def plot_pay_pan(pay_set: np.ndarray, pan_faces: np.ndarray):
    ts = np.arange(len(pay_set))
    sides = side_panel(pan_faces, method="max")
    plt.figure()
    plt.plot(ts, pay_set, color="green", label="Payload")
    plt.plot(ts, sides, color="red", label="Side panel")
    plt.title("Payload and side panel light values")
    plt.xlabel("Timestamp index")
    plt.ylabel("Light value")
    plt.legend()


def plot_on_off(pay_set: np.ndarray, labels: np.ndarray):
    ts = np.arange(len(pay_set))
    plt.figure()
    for start, end, label in find_blocks(labels):
        plt.plot(ts[start:end], pay_set[start:end],
                 color="green" if label else "red",
                 label="On" if label else "Off")
    # Deduplicate legend entries
    handles, hlabels = plt.gca().get_legend_handles_labels()
    seen = {}
    for h, l in zip(handles, hlabels):
        seen.setdefault(l, h)
    plt.legend(seen.values(), seen.keys())
    plt.title("Payload ON/OFF light values")
    plt.xlabel("Timestamp index")
    plt.ylabel("Light value")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading data from", DB_PATH)
    timestamps, payl_light, pan_faces, pay_set_labels = load_data()
    n = len(payl_light)
    print(f"  {n} packets with light data")
    print(f"  ON packets: {pay_set_labels.sum()}  OFF packets: {(~pay_set_labels).sum()}")

    panels = side_panel(pan_faces, method="max")

    # Test 1: payload vs side panel (all four statistics)
    print("\n-- Test 1: Payload vs side panel --")
    for stat in _TEST_STATS:
        result = paired_ptest(payl_light, panels, statistic=stat)
        print(f"  {result['test']:30s}  observed={result['observed']:.4f}  p={result['p_value']:.4f}")

    # Test 2: payload ON vs OFF
    if pay_set_labels.sum() == 0 or (~pay_set_labels).sum() == 0:
        print("\n-- Test 2 skipped: all packets have the same payload state --")
    else:
        print("\n-- Test 2: Payload ON vs OFF --")
        for stat in _TEST_STATS:
            result = block_ptest(payl_light, pay_set_labels, statistic=stat)
            print(f"  {result['test']:30s}  observed={result['observed']:.4f}  p={result['p_value']:.4f}")

    plot_pay_pan(payl_light, pan_faces)
    plot_on_off(payl_light, pay_set_labels)
    plt.show()


if __name__ == "__main__":
    main()
