"""Shared device registry loader, schema/invariant validator, and tier dispatch guard.

Loads devices/devices.json as the single source of truth for Python drivers,
HID discovery, CLI write-gating, and udev rule generation.
"""
import copy
import functools
import json
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = REPO_ROOT / "devices" / "devices.json"
DEFAULT_SCHEMA_PATH = REPO_ROOT / "devices" / "schema.json"

REQUIRED_OPERATIONS = (
    "firmware",
    "serial",
    "dpi",
    "stages",
    "polling",
    "lod",
    "battery",
    "power",
    "buttons",
    "layers",
    "dongle",
    "lighting",
    "tuning",
)

HEX4_RE = re.compile(r"^[0-9a-f]{4}$")
HEX2_RE = re.compile(r"^0x[0-9a-f]{2}$")
REV_RE = re.compile(r"^[0-9a-f]{7,40}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

TIER_LABELS = {
    0: "Tier 0 · Pointer only",
    1: "Tier 1 · Read-only",
    2: "Tier 2 · Verified",
}


class ReadOnlyTierError(PermissionError):
    """Raised when a mutating or unallowlisted HID command is dispatched to a Tier 0/1 device."""


def parse_hex_int(value, default=None):
    if value is None:
        return default
    if isinstance(value, int):
        return value
    return int(str(value), 16)


def tier_label(tier):
    return TIER_LABELS.get(int(tier), TIER_LABELS[0])


def load_registry_raw(path=None):
    reg_path = Path(path or DEFAULT_REGISTRY_PATH)
    with reg_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_registry(doc):
    """Validate registry schema, provenance pins, distinct namespaces, and tier safety invariants.

    Returns a list of error strings (empty list means valid).
    """
    errors = []
    if not isinstance(doc, dict):
        return ["registry root must be a JSON object"]

    if doc.get("schema_version") != 1:
        errors.append(f"unsupported schema_version: {doc.get('schema_version')!r}")

    tiers = doc.get("tiers")
    if not isinstance(tiers, dict) or set(tiers.keys()) != {"0", "1", "2"}:
        errors.append("tiers must define exact keys '0', '1', '2'")

    sources = doc.get("sources")
    if not isinstance(sources, dict) or not sources:
        errors.append("sources must be a non-empty object")
        sources = {}
    else:
        for skey, src in sources.items():
            if not isinstance(src, dict):
                errors.append(f"source {skey!r} must be an object")
                continue
            url = src.get("url", "")
            rev = src.get("revision", "")
            if not isinstance(url, str) or not url.startswith("https://"):
                errors.append(f"source {skey!r} url must start with https://")
            if not isinstance(rev, str) or not REV_RE.match(rev):
                errors.append(f"source {skey!r} revision must be a 7-40 char hex commit pin")
            for req in ("path", "licence", "extraction_rule"):
                if not isinstance(src.get(req), str) or not src[req].strip():
                    errors.append(f"source {skey!r} missing non-empty {req}")

    models = doc.get("models")
    model_by_key = {}
    if not isinstance(models, list) or not models:
        errors.append("models must be a non-empty list")
    else:
        for m in models:
            if not isinstance(m, dict):
                errors.append("model entry must be an object")
                continue
            key = m.get("key")
            if not isinstance(key, str) or not SLUG_RE.match(key):
                errors.append(f"invalid model key {key!r}")
                continue
            if key in model_by_key:
                errors.append(f"duplicate model key {key!r}")
            model_by_key[key] = m
            if m.get("kind") not in ("mouse", "receiver", "keyboard"):
                errors.append(f"model {key!r} invalid kind {m.get('kind')!r}")
            if m.get("source") not in sources:
                errors.append(f"model {key!r} cites unknown source {m.get('source')!r}")

    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        errors.append("profiles must be a non-empty object")
        profiles = {}
    else:
        for pid_key, prof in profiles.items():
            if not isinstance(prof, dict):
                errors.append(f"profile {pid_key!r} must be an object")
                continue
            tier = prof.get("tier")
            write_enabled = prof.get("write_enabled")
            legacy = prof.get("legacy_migrated")
            ev_status = prof.get("evidence_status")
            evidence = prof.get("evidence")
            if tier not in (0, 1, 2):
                errors.append(f"profile {pid_key!r} invalid tier {tier!r}")
            if not isinstance(write_enabled, bool):
                errors.append(f"profile {pid_key!r} write_enabled must be bool")
            if not isinstance(legacy, bool):
                errors.append(f"profile {pid_key!r} legacy_migrated must be bool")
            if prof.get("source") not in sources:
                errors.append(f"profile {pid_key!r} cites unknown source {prof.get('source')!r}")

            # Safety invariant: Tier 0 and Tier 1 NEVER allow writes
            if tier in (0, 1) and write_enabled:
                errors.append(f"profile {pid_key!r} is Tier {tier} but has write_enabled=true")

            # Safety invariant: no silent promotion of legacy entries or unverified new profiles
            if ev_status == "hardware_verified":
                if not isinstance(evidence, dict):
                    errors.append(f"profile {pid_key!r} claims hardware_verified without evidence record")
                else:
                    for req in ("firmware", "backend", "transport", "date", "result"):
                        if not isinstance(evidence.get(req), str) or not evidence[req].strip():
                            errors.append(f"profile {pid_key!r} evidence missing {req}")
                    if isinstance(evidence.get("date"), str) and not DATE_RE.match(evidence["date"]):
                        errors.append(f"profile {pid_key!r} evidence date must be YYYY-MM-DD")
            if legacy and evidence is None and ev_status == "hardware_verified":
                errors.append(f"legacy profile {pid_key!r} was silently promoted to hardware_verified")
            if not legacy and (tier == 2 or write_enabled) and evidence is None:
                errors.append(f"non-legacy profile {pid_key!r} cannot be Tier 2 / write_enabled without hardware evidence")

            ops = prof.get("operations")
            if not isinstance(ops, dict) or set(ops.keys()) != set(REQUIRED_OPERATIONS):
                errors.append(f"profile {pid_key!r} operations must define exact required keys")
            else:
                for op_name, op in ops.items():
                    if not isinstance(op, dict) or not isinstance(op.get("read"), bool) or not isinstance(op.get("write"), bool):
                        errors.append(f"profile {pid_key!r} op {op_name!r} must have boolean read/write")
                        continue
                    if tier == 0 and (op["read"] or op["write"]):
                        errors.append(f"Tier 0 profile {pid_key!r} op {op_name!r} must have read=false, write=false")
                    if tier == 1 and op["write"]:
                        errors.append(f"Tier 1 profile {pid_key!r} op {op_name!r} must have write=false")
                    if not write_enabled and op["write"]:
                        errors.append(f"read-only profile {pid_key!r} op {op_name!r} has write=true")
                    if "txn" in op and not (isinstance(op["txn"], str) and HEX2_RE.match(op["txn"])):
                        errors.append(f"profile {pid_key!r} op {op_name!r} invalid txn {op['txn']!r}")
                    if "storage" in op and not (isinstance(op["storage"], str) and HEX2_RE.match(op["storage"])):
                        errors.append(f"profile {pid_key!r} op {op_name!r} invalid storage {op['storage']!r}")
                    if "claim_source" in op and op["claim_source"] not in sources:
                        errors.append(f"profile {pid_key!r} op {op_name!r} cites unknown claim_source {op['claim_source']!r}")

    matches = doc.get("matches")
    if not isinstance(matches, list) or not matches:
        errors.append("matches must be a non-empty list")
    else:
        seen_ids = set()
        seen_ns_keys = {}
        for m in matches:
            if not isinstance(m, dict):
                errors.append("match entry must be an object")
                continue
            mid = m.get("id")
            if mid in seen_ids:
                errors.append(f"duplicate match id {mid!r}")
            seen_ids.add(mid)

            mk = m.get("model_key")
            pk = m.get("profile_id")
            if mk not in model_by_key:
                errors.append(f"match {mid!r} references unknown model_key {mk!r}")
            if pk not in profiles:
                errors.append(f"match {mid!r} references unknown profile_id {pk!r}")
            if m.get("source") not in sources:
                errors.append(f"match {mid!r} cites unknown source {m.get('source')!r}")

            bus = m.get("bus")
            ns = m.get("id_namespace")
            vid = m.get("vid")
            pid = m.get("pid")
            wpid = m.get("wpid")

            if not (isinstance(vid, str) and HEX4_RE.match(vid)):
                errors.append(f"match {mid!r} invalid vid {vid!r}")

            # Enforce strict bus <-> id_namespace <-> pid/wpid separation
            if bus == "usb":
                if ns != "usb_pid":
                    errors.append(f"match {mid!r} usb bus must use id_namespace 'usb_pid'")
                if not (isinstance(pid, str) and HEX4_RE.match(pid)) or wpid is not None:
                    errors.append(f"match {mid!r} usb_pid requires hex4 pid and wpid=null")
            elif bus == "bluetooth":
                if ns != "bluetooth_pid":
                    errors.append(f"match {mid!r} bluetooth bus must use id_namespace 'bluetooth_pid'")
                if not (isinstance(pid, str) and HEX4_RE.match(pid)) or wpid is not None:
                    errors.append(f"match {mid!r} bluetooth_pid requires hex4 pid and wpid=null")
                if m.get("udev_eligible"):
                    errors.append(f"match {mid!r} bluetooth entry must not be udev_eligible")
            elif bus == "receiver_child":
                if ns != "logitech_wpid":
                    errors.append(f"match {mid!r} receiver_child bus must use id_namespace 'logitech_wpid'")
                if not (isinstance(wpid, str) and HEX4_RE.match(wpid)) or pid is not None:
                    errors.append(f"match {mid!r} logitech_wpid requires hex4 wpid and pid=null")
                if m.get("udev_eligible"):
                    errors.append(f"match {mid!r} WPID entry must not be udev_eligible")
            else:
                errors.append(f"match {mid!r} invalid bus {bus!r}")

            # No duplicate keys within the same namespace
            ns_id = (ns, vid, wpid if ns == "logitech_wpid" else pid)
            if ns_id in seen_ns_keys:
                errors.append(f"duplicate namespace key {ns_id!r} in match {mid!r} and {seen_ns_keys[ns_id]!r}")
            seen_ns_keys[ns_id] = mid

            # udev eligibility safety checks
            model = model_by_key.get(mk)
            prof = profiles.get(pk)
            if m.get("udev_eligible"):
                if model and model.get("kind") == "keyboard":
                    errors.append(f"match {mid!r} keyboard model must not be udev_eligible")
                if prof and prof.get("tier", 0) < 1:
                    errors.append(f"match {mid!r} Tier 0 profile must not be udev_eligible")

    return errors


class DeviceRegistry:
    """Indexed read-only view of devices/devices.json."""

    def __init__(self, doc):
        errs = validate_registry(doc)
        if errs:
            raise ValueError("Invalid device registry:\n  - " + "\n  - ".join(errs))
        self.doc = doc
        self.models = {m["key"]: m for m in doc["models"]}
        self.profiles = doc["profiles"]
        self.matches = doc["matches"]
        self.sources = doc["sources"]
        self.atk_products = doc.get("atk_products", [])

        self._by_usb = {}
        self._by_bt = {}
        self._by_wpid = {}
        for m in self.matches:
            vid = int(m["vid"], 16)
            ns = m["id_namespace"]
            if ns == "usb_pid":
                self._by_usb[(vid, int(m["pid"], 16))] = m
            elif ns == "bluetooth_pid":
                self._by_bt[(vid, int(m["pid"], 16))] = m
            elif ns == "logitech_wpid":
                self._by_wpid[(vid, int(m["wpid"], 16))] = m

    def resolve_match(self, vid, pid, bus="usb"):
        """Return enriched (match, model, profile) for (vid, pid) on the given bus, or None."""
        m = self._by_bt.get((vid, pid)) if bus == "bluetooth" else self._by_usb.get((vid, pid))
        if m is None and bus == "usb":
            # Never cross-lookup WPIDs; bluetooth_pid only matches bluetooth bus
            return None
        if m is None:
            return None
        return m, self.models[m["model_key"]], self.profiles[m["profile_id"]]

    def find_match_any_bus(self, vid, pid):
        """Return (match, model, profile) checking usb_pid then bluetooth_pid (never logitech_wpid)."""
        m = self._by_usb.get((vid, pid)) or self._by_bt.get((vid, pid))
        if m is None:
            return None
        return m, self.models[m["model_key"]], self.profiles[m["profile_id"]]

    def udev_usb_matches(self):
        """Return reviewed USB control-endpoint matches eligible for udev rules, sorted deterministically."""
        out = []
        seen = set()
        for m in self.matches:
            if not m.get("udev_eligible"):
                continue
            if m.get("bus") != "usb" or m.get("id_namespace") != "usb_pid":
                continue
            model = self.models[m["model_key"]]
            prof = self.profiles[m["profile_id"]]
            if model.get("kind") == "keyboard":
                continue
            if prof.get("tier", 0) < 1:
                continue
            vp = (m["vid"], m["pid"])
            if vp in seen:
                continue
            seen.add(vp)
            out.append((m, model, prof))
        return out

    # --- Backward-compatible table builders for Python drivers ---

    def build_razer_devices(self):
        out = {}
        for m in self.matches:
            if m["id_namespace"] != "usb_pid" or int(m["vid"], 16) != 0x1532:
                continue
            prof = self.profiles[m["profile_id"]]
            if prof["driver"] != "razer" or prof["tier"] < 1:
                continue
            model = self.models[m["model_key"]]
            pid = int(m["pid"], 16)
            dpi_max = m.get("meta", {}).get("dpi_max", prof["operations"]["dpi"].get("dpi_max", 30000))
            out[pid] = (model["name"], m["link"], m["prio"], m["wireless"], m["hyper"], dpi_max)
        return out

    def build_egg_devices(self):
        out = {}
        for m in self.matches:
            if m["id_namespace"] != "usb_pid" or int(m["vid"], 16) != 0x3367:
                continue
            prof = self.profiles[m["profile_id"]]
            if prof["driver"] != "egg8k":
                continue
            model = self.models[m["model_key"]]
            meta = copy.deepcopy(m.get("meta", {}))
            meta["name"] = model["name"]
            out[int(m["pid"], 16)] = meta
        return out

    def build_egg_we_devices(self):
        out = {}
        for m in self.matches:
            if m["id_namespace"] != "usb_pid" or int(m["vid"], 16) != 0x3367:
                continue
            prof = self.profiles[m["profile_id"]]
            if prof["driver"] != "egg_we":
                continue
            model = self.models[m["model_key"]]
            out[int(m["pid"], 16)] = (model["name"], m["link"], m["wireless"], m["prio"])
        return out

    def build_logi_devices(self):
        out = {}
        for m in self.matches:
            if m["id_namespace"] not in ("usb_pid", "bluetooth_pid") or int(m["vid"], 16) != 0x046D:
                continue
            prof = self.profiles[m["profile_id"]]
            if prof["driver"] != "hidpp":
                continue
            model = self.models[m["model_key"]]
            out[int(m["pid"], 16)] = (model["name"], m["link"])
        return out

    def build_pulsar_xs1_devices(self):
        out = {}
        for m in self.matches:
            if m["id_namespace"] != "usb_pid" or int(m["vid"], 16) != 0x3710:
                continue
            prof = self.profiles[m["profile_id"]]
            if prof["driver"] != "pulsar_xs1":
                continue
            model = self.models[m["model_key"]]
            max_poll = m.get("meta", {}).get("max_poll", 1000)
            out[int(m["pid"], 16)] = (model["name"], m["link"], m["wireless"], max_poll)
        return out

    def build_pulsar_pro_devices(self):
        out = {}
        for m in self.matches:
            if m["id_namespace"] != "usb_pid" or int(m["vid"], 16) != 0x3710:
                continue
            prof = self.profiles[m["profile_id"]]
            if prof["driver"] != "pulsar_pro":
                continue
            model = self.models[m["model_key"]]
            max_poll = m.get("meta", {}).get("max_poll", 8000)
            out[int(m["pid"], 16)] = (model["name"], m["link"], m["wireless"], max_poll)
        return out

    def build_areson_devices(self):
        out = {}
        for m in self.matches:
            if m["id_namespace"] != "usb_pid" or int(m["vid"], 16) != 0x25A7:
                continue
            prof = self.profiles[m["profile_id"]]
            if m["profile_id"] != "pulsar_areson_compx":
                continue
            model = self.models[m["model_key"]]
            out[int(m["pid"], 16)] = (model["name"], m["link"], m["wireless"])
        return out

    def build_compx_devices(self):
        out = {}
        for m in self.matches:
            if m["id_namespace"] != "usb_pid":
                continue
            if m["profile_id"] != "compx_odm":
                continue
            model = self.models[m["model_key"]]
            meta = m.get("meta", {})
            rec = {
                "brand": meta["brand"],
                "name": model["name"],
                "link": m["link"],
                "wireless": m["wireless"],
                "dpi_max": meta["dpi_max"],
                "rates": list(meta["rates"]),
            }
            if meta.get("model"):
                rec["model"] = meta["model"]
            out[(int(m["vid"], 16), int(m["pid"], 16))] = rec
        return out

    def build_atk_products(self):
        out = {}
        for item in self.atk_products:
            rec = {
                "name": item["name"],
                "sensor": item["sensor"],
                "dpi_max": item["dpi_max"],
                "rates": list(item["rates"]),
                "max_stages": item["max_stages"],
                "is_r1": bool(item.get("is_r1", False)),
            }
            if item.get("dpi6"):
                rec["dpi6"] = True
            if item.get("anti_mistouch"):
                rec["anti_mistouch"] = True
            out[(int(item["cid"]), int(item["mid"]))] = rec
        return out

    def build_glorious_tables(self):
        sino, core2, classic, pixart = {}, {}, {}, {}
        for m in self.matches:
            if m["id_namespace"] != "usb_pid":
                continue
            vp = (int(m["vid"], 16), int(m["pid"], 16))
            model = self.models[m["model_key"]]
            meta = m.get("meta", {})
            pid_key = m["profile_id"]
            if pid_key == "glorious_sinowealth":
                sino[vp] = (model["name"], m["link"])
            elif pid_key == "glorious_core2":
                core2[vp] = (model["name"], m["link"], m["wireless"], meta.get("max_poll", 4000))
            elif pid_key == "glorious_classic":
                classic[vp] = {
                    "name": model["name"],
                    "link": m["link"],
                    "wireless": m["wireless"],
                    "gen": meta.get("gen", "legacy"),
                }
            elif pid_key == "glorious_pixart":
                pixart[vp] = (model["name"], m["link"], m["wireless"])
        return sino, core2, classic, pixart


@functools.lru_cache(maxsize=4)
def get_registry(path=None):
    return DeviceRegistry(load_registry_raw(path))


def operation_spec(meta, op_name):
    ops = (meta or {}).get("operations") or {}
    return ops.get(op_name) or {"read": False, "write": False}


def assert_operation_allowed(meta, op_name, write=False):
    """Enforce Tier 0/1/2 capability and write-blocking rules at dispatch."""
    if not meta:
        return
    tier = meta.get("tier", 2)
    name = meta.get("name") or meta.get("device") or "this device"
    if tier == 0:
        raise ReadOnlyTierError(
            f"Blocked: {name} is {tier_label(0)}; vendor HID {op_name} commands are disabled"
        )
    if write and (tier < 2 or not meta.get("write_enabled", tier >= 2)):
        raise ReadOnlyTierError(
            f"Blocked: {name} is {tier_label(tier)}; mutating {op_name} commands are blocked at dispatch"
        )
    ops = meta.get("operations")
    if ops is not None:
        spec = ops.get(op_name) or {"read": False, "write": False}
        flag = "write" if write else "read"
        if not spec.get(flag, False):
            kind = "write" if write else "read"
            raise ReadOnlyTierError(
                f"Blocked: {name} ({tier_label(tier)}) does not permit {op_name} {kind}"
            )


def is_razer_cmd_allowed(meta, cls, cmd, txn=None):
    """Check whether a raw Razer (cls, cmd, txn) frame is allowlisted for a Tier 0/1 device."""
    if not meta:
        return True
    tier = meta.get("tier", 2)
    if tier == 0:
        return False
    if tier >= 2 and meta.get("write_enabled", True):
        return True
    # Tier 1: strict command + transaction byte allowlist derived from profile operations
    ops = meta.get("operations") or {}
    for op in ops.values():
        if not op.get("read") or op.get("write"):
            continue
        if "cmd_class" not in op or "cmd_id" not in op:
            continue
        op_cls = parse_hex_int(op["cmd_class"])
        op_cmd = parse_hex_int(op["cmd_id"])
        op_txn = parse_hex_int(op.get("txn"), 0x1F)
        if (cls, cmd) == (op_cls, op_cmd) and (txn is None or txn == op_txn):
            return True
    return False
