#!/usr/bin/env python3
"""Deterministically generate platform/linux/60-mousetool.rules from devices/devices.json.

Only reviewed USB control-endpoint match records (bus == "usb", id_namespace == "usb_pid",
udev_eligible == true, model.kind != "keyboard", profile.tier >= 1) emit hidraw uaccess rules.
WPIDs, Bluetooth-only entries, unresolved Tier 0 catalog entries, and keyboards never emit rules.
"""
import argparse
import difflib
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mousetool.registry import DEFAULT_REGISTRY_PATH, DeviceRegistry, load_registry_raw, tier_label

DEFAULT_OUTPUT_PATH = REPO_ROOT / "platform" / "linux" / "60-mousetool.rules"

GROUP_ORDER = [
    "Razer (VID 1532)",
    "Endgame Gear 8k (VID 3367)",
    "Logitech HID++ USB mice & receivers (VID 046d)",
    "Endgame Gear -we (VID 3367)",
    "Pulsar (VID 3710, 25a7, 3554)",
    "CompX ODM Ecosystem (VID 3554, 373b)",
    "Glorious (VID 258a, 22d4, 320f, 3794, 093a)",
]


def render_udev_rules(registry_doc):
    reg = DeviceRegistry(registry_doc)
    eligible = reg.udev_usb_matches()

    grouped = {}
    for match, model, profile in eligible:
        group = match.get("udev_group") or f"{model['brand'].title()} (VID {match['vid']})"
        grouped.setdefault(group, []).append((match, model, profile))

    ordered_groups = [g for g in GROUP_ORDER if g in grouped]
    ordered_groups.extend(sorted(g for g in grouped if g not in ordered_groups))

    lines = [
        "# Mouse Tool: let the logged-in user open hidraw nodes (for WebHID / mousetool)",
        "# Generated deterministically by tools/generate_udev_rules.py from devices/devices.json.",
        "# Only reviewed USB control-endpoint matches (Tier >= 1, non-keyboard) emit rules.",
        "# WPIDs, Bluetooth-only entries, Tier 0 catalog entries, and keyboards are excluded.",
    ]

    for group in ordered_groups:
        lines.append("")
        lines.append(f"# --- {group} ---")
        entries = sorted(grouped[group], key=lambda item: (item[0]["vid"], item[0]["pid"]))
        for match, model, profile in entries:
            vid = match["vid"]
            pid = match["pid"]
            t_note = f", {tier_label(profile['tier'])}" if profile["tier"] == 1 else ""
            lines.append(f"# {model['name']} ({match['link']}{t_note})")
            lines.append(
                f'KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vid}", ATTRS{{idProduct}}=="{pid}", '
                f'MODE="0660", TAG+="uaccess"'
            )

    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY_PATH), help="Path to devices.json")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Path to 60-mousetool.rules")
    ap.add_argument("--check", action="store_true", help="Exit non-zero if output differs from generated rules")
    ap.add_argument("--stdout", action="store_true", help="Write generated rules to stdout instead of file")
    args = ap.parse_args(argv)

    doc = load_registry_raw(args.registry)
    generated = render_udev_rules(doc)

    if args.stdout:
        sys.stdout.write(generated)
        return 0

    out_path = Path(args.output)
    if args.check:
        if not out_path.exists():
            sys.stderr.write(f"ERROR: missing udev rules file: {out_path}\n")
            return 1
        existing = out_path.read_text(encoding="utf-8")
        if existing != generated:
            diff = "".join(difflib.unified_diff(
                existing.splitlines(keepends=True),
                generated.splitlines(keepends=True),
                fromfile=str(out_path),
                tofile="generated",
            ))
            sys.stderr.write(f"ERROR: {out_path} is out of date with {args.registry}:\n{diff}")
            return 1
        return 0

    out_path.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
