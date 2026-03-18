"""
Mission Analysis — Post-mission data exploration & export.

Run interactively to inspect the packet database, plot telemetry
time-series, or export data to CSV for external analysis.

Usage::

    python mission_analysis.py              # print summary
    python mission_analysis.py --export     # dump CSVs to ./exports/
"""

import argparse
import os
from pathlib import Path

import packet_store


def print_summary():
    s = packet_store.summary()
    print("=" * 52)
    print("  MISSION DATA SUMMARY")
    print("=" * 52)
    print(f"  Total packets        : {s['total_packets']}")
    print(f"  Total telemetry rows : {s['total_telemetry_rows']}")
    print(f"  Earliest packet      : {s['earliest_packet'] or '—'}")
    print(f"  Latest packet        : {s['latest_packet'] or '—'}")
    if s["telemetry_keys"]:
        print(f"  Telemetry keys       : {', '.join(s['telemetry_keys'])}")
    else:
        print("  Telemetry keys       : (none)")
    print("=" * 52)


def print_recent(n: int = 10):
    pkts = packet_store.recent_packets(n=n)
    if not pkts:
        print("No packets in database.")
        return
    print(f"\n  Last {len(pkts)} packets:")
    print(f"  {'ID':>6}  {'Time':<26}  {'Satellite':<14}  {'Station':<20}  {'RSSI':>6}  {'SNR':>5}")
    print("  " + "-" * 85)
    for p in pkts:
        print(
            f"  {p['id']:>6}  {p['received_at']:<26}  "
            f"{(p['satellite'] or '?'):<14}  {(p['station'] or '?'):<20}  "
            f"{p['rssi'] or '':>6}  {p['snr'] or '':>5}"
        )


def export_all(out_dir: str = "exports"):
    Path(out_dir).mkdir(exist_ok=True)
    pkt_path = os.path.join(out_dir, "packets.csv")
    tel_path = os.path.join(out_dir, "telemetry.csv")
    packet_store.export_packets_csv(pkt_path)
    packet_store.export_telemetry_csv(tel_path)
    print(f"Exported packets   -> {pkt_path}")
    print(f"Exported telemetry -> {tel_path}")


def plot_telemetry(key: str):
    """Quick matplotlib plot of a telemetry key over time."""
    series = packet_store.telemetry_series(key)
    if not series:
        print(f"No telemetry data for key '{key}'.")
        return

    import matplotlib.pyplot as plt
    from datetime import datetime

    times = [datetime.fromisoformat(r["timestamp"]) for r in series]
    values = [r["value"] for r in series]
    unit = series[0].get("unit", "")

    plt.figure(figsize=(12, 4), facecolor="#060e1a")
    ax = plt.gca()
    ax.set_facecolor("#0a1628")
    ax.plot(times, values, color="#00ccff", linewidth=0.8)
    ax.set_title(f"Telemetry: {key}", color="#00ddff", fontfamily="monospace")
    ax.set_ylabel(f"{key} ({unit})" if unit else key,
                  color="#8899aa", fontfamily="monospace")
    ax.tick_params(colors="#667788", labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#1a3a50")
    ax.grid(True, color="white", alpha=0.08)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mission data analysis")
    parser.add_argument("--export", action="store_true",
                        help="Export packets and telemetry to CSV")
    parser.add_argument("--recent", type=int, default=0,
                        help="Show N most recent packets")
    parser.add_argument("--plot", type=str, default="",
                        help="Plot a telemetry key (e.g. 'temperature')")
    args = parser.parse_args()

    print_summary()

    if args.recent > 0:
        print_recent(args.recent)
    if args.export:
        export_all()
    if args.plot:
        plot_telemetry(args.plot)
