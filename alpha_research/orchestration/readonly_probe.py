"""Explicitly authorized, narrowly scoped BRAIN read-only connectivity probe.

The caller must grant this probe separately via --allow-brain-read. The
Gateway and its driver both enforce the same two-command whitelist. The
authentication POST is the platform's required login handshake; no
simulation POST, Alpha tag, upload or submission can be issued here.
"""
import re

from alpha_research.integrations.brain.gateway import READONLY_PROBE_COMMANDS

_ALLOWED = frozenset({"get_platform_setting_options", "readonly_probe_datasets"})
if READONLY_PROBE_COMMANDS != _ALLOWED:
    raise RuntimeError("read-only Gateway contract drift")


def run_readonly_probe(gateway, *, region, delay, universe):
    if (not isinstance(region, str)
            or re.fullmatch(r"[A-Z]{3,8}", region) is None):
        raise ValueError("read-only probe requires an exact Region")
    if type(delay) is not int or delay not in (0, 1):
        raise ValueError("read-only probe requires Delay 0 or 1")
    if (not isinstance(universe, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,40}", universe) is None):
        raise ValueError("read-only probe requires an exact Universe")
    if getattr(gateway, "read_only", None) is not True:
        raise ValueError("read-only probe requires a restricted Gateway")
    options = gateway.call("get_platform_setting_options")
    if not isinstance(options, dict) or not isinstance(
            options.get("instrument_options"), list):
        raise ValueError("BRAIN platform settings are unavailable")
    match = False
    for row in options["instrument_options"]:
        if (isinstance(row, dict)
                and row.get("InstrumentType") == "EQUITY"
                and row.get("Region") == region
                and row.get("Delay") == delay
                and isinstance(row.get("Universe"), list)
                and universe in row["Universe"]):
            match = True
            break
    if not match:
        return {
            "status": "SCOPE_NOT_AVAILABLE",
            "region": region, "delay": delay, "universe": universe,
            "platform_settings_readable": True, "dataset_catalog_readable": False,
            "brain_read_invoked": True, "simulation_invoked": False,
            "model_invoked": False, "alpha_submission_invoked": False,
        }
    datasets = gateway.call(
        "readonly_probe_datasets", region=region, delay=delay,
        universe=universe, instrument_type="EQUITY")
    if (not isinstance(datasets, dict)
            or datasets.get("catalog_readable") is not True
            or type(datasets.get("dataset_count")) is not int
            or datasets["dataset_count"] < 0):
        raise ValueError("BRAIN dataset probe response is invalid")
    return {
        "status": "READ_ONLY_SCOPE_VERIFIED",
        "region": region, "delay": delay, "universe": universe,
        "platform_settings_readable": True,
        "dataset_catalog_readable": True,
        "dataset_count": datasets["dataset_count"],
        "brain_read_invoked": True, "simulation_invoked": False,
        "model_invoked": False, "alpha_submission_invoked": False,
    }
