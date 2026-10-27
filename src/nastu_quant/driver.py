"""nastu_quant BRAIN driver: persistent process, whitelist-only platform access.

Implements AGENT_DESIGN.md §4.1 / §6.1 / §9 (adapted from the legacy
workspace brain_driver.py with three changes):

  1. Session cookie lives INSIDE the project state dir
     (env NASTU_QUANT_STATE_DIR, default ~/workspace/nastu_quant/state),
     file session_cookie.json (0600). needs_auth marker alongside it.
  2. Auth cascade (FR-01): cookie -> credential file -> needs_auth.
     The credential file lives OUTSIDE the project; its path comes from
     env NASTU_QUANT_CREDENTIAL_FILE and it holds JSON
     {"email": ..., "password": ...}. The driver reads it in place;
     it is never copied, logged, or echoed. The `reauth` command takes
     NO credential parameters: it re-reads the credential file itself,
     so credentials never travel through the command protocol.
  3. platform_functions location is overridable via
     env NASTU_QUANT_PF_PATH (default: the brain_env install).

submit_alpha is deliberately NOT exposed (FR-02 red line: submission
must be impossible at the capability layer, not just by convention).

Line-delimited JSON protocol: {"cmd": ..., params...} -> {"ok": bool, ...}
"""
import sys
import sysconfig
import json
import asyncio
import os
import io
import re
from contextlib import redirect_stdout
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from urllib.parse import urlencode
from pagination import collect_pages

STATE_DIR = Path(os.environ.get(
    "NASTU_QUANT_STATE_DIR",
    str(Path.home() / "workspace" / "nastu_quant" / "state"),
))
COOKIE_FILE = STATE_DIR / "session_cookie.json"
NEEDS_AUTH_MARKER = STATE_DIR / "needs_auth"
CREDENTIAL_FILE = os.environ.get("NASTU_QUANT_CREDENTIAL_FILE", "")
PF_PATH = os.environ.get(
    "NASTU_QUANT_PF_PATH",
    str(Path(sysconfig.get_paths()["purelib"]) / "cnhkmcp" / "untracked"
        / "platform_functions.py"),
)
STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_pf():
    spec = spec_from_file_location("_wspf", PF_PATH)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def save_cookie(pf):
    """Persist current session cookies to the in-project cookie file (0600)."""
    try:
        client = pf.brain_client
        cookies = client.session.cookies
        wq_cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in cookies
            if "worldquantbrain" in (c.domain or "")
        ]
        if not wq_cookies:
            return False
        COOKIE_FILE.write_text(json.dumps({"cookies": wq_cookies}), encoding="utf-8")
        os.chmod(COOKIE_FILE, 0o600)
        NEEDS_AUTH_MARKER.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def try_cookie(pf, loop):
    """Attempt to restore the session from the cookie file. True if valid."""
    try:
        if not COOKIE_FILE.exists():
            return False
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        client = pf.brain_client
        client.session.cookies.clear()
        for c in data.get("cookies", []):
            client.session.cookies.set(
                c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/")
            )
        ok = loop.run_until_complete(client.is_authenticated())
        return bool(ok)
    except Exception:
        return False


def read_credential_file():
    """Read {email, password} from the external credential file, or None.

    The file is read in place via the env-injected path; its contents are
    never copied into the project, logged, or returned to callers.
    """
    if not CREDENTIAL_FILE:
        return None
    try:
        data = json.loads(Path(CREDENTIAL_FILE).read_text(encoding="utf-8"))
        email, password = data.get("email", ""), data.get("password", "")
        if email and password:
            return {"email": email, "password": password}
    except Exception:
        return None
    return None


def main():
    pf = load_pf()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def call_sync(fn, params):
        # Some platform functions print progress while polling. Keep stdout
        # reserved for one JSON response per request.
        with redirect_stdout(io.StringIO()):
            res = fn(**params)
            if asyncio.iscoroutine(res):
                res = loop.run_until_complete(res)
        return res

    def get_dataset_details(dataset_id):
        if not isinstance(dataset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", dataset_id):
            raise ValueError("invalid Dataset ID")
        response = pf.brain_client.session.get(
            f"{pf.brain_client.base_url}/data-sets/{dataset_id}", timeout=40)
        response.raise_for_status()
        return response.json()

    def get_simulation_settings_schema():
        response = pf.brain_client.session.options(
            f"{pf.brain_client.base_url}/simulations", timeout=40)
        response.raise_for_status()
        return response.json()["actions"]["POST"]["settings"]["children"]

    def authenticate(creds):
        call_sync(pf.authenticate, {"email": creds["email"], "password": creds["password"]})
        save_cookie(pf)

    authenticated_via = None

    # Level 1: cookie file (FR-01).
    if try_cookie(pf, loop):
        authenticated_via = "cookie"
    else:
        # Level 2: credential file (FR-01). Never stdin in the new system:
        # unattended runs must fail closed with needs_auth, not block.
        creds = read_credential_file()
        if creds is None:
            NEEDS_AUTH_MARKER.write_text(
                "cookie invalid and credential file unavailable\n")
            sys.stdout.write(json.dumps({
                "ok": False, "stage": "auth",
                "error": "needs_auth: cookie invalid, credential file unavailable",
            }) + "\n")
            sys.stdout.flush()
            return
        try:
            authenticate(creds)
        except Exception as e:
            msg = str(e)
            NEEDS_AUTH_MARKER.write_text("credential file authentication failed\n")
            sys.stdout.write(json.dumps({
                "ok": False, "stage": "auth", "error": msg[:600],
                "throttled": "429" in msg or "THROTTLED" in msg,
                "persona": "persona" in msg.lower(),
            }) + "\n")
            sys.stdout.flush()
            return
        authenticated_via = "credential_file"

    sys.stdout.write(json.dumps({"ok": True, "stage": "authenticated",
                                 "via": authenticated_via}) + "\n")
    sys.stdout.flush()

    allowed = {
        "get_platform_setting_options": pf.get_platform_setting_options,
        "get_simulation_settings_schema": get_simulation_settings_schema,
        "get_datafields": pf.get_datafields,
        "get_datasets": pf.get_datasets,
        "get_dataset_details": get_dataset_details,
        "get_operators": pf.get_operators,
        "create_simulation": pf.create_simulation,
        "create_multi_simulation": pf.create_multi_simulation,
        "get_alpha_details": pf.get_alpha_details,
        "get_alpha_pnl": pf.get_alpha_pnl,
        "get_alpha_yearly_stats": pf.get_alpha_yearly_stats,
        "check_correlation": pf.check_correlation,
        "get_user_alphas": pf.get_user_alphas,
        "reauth": None,  # special: re-read credential file, no params
    }

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except Exception as e:
            sys.stdout.write(json.dumps({"ok": False, "error": "parse: " + str(e)[:200]}) + "\n")
            sys.stdout.flush()
            continue
        if cmd.get("cmd") == "shutdown":
            sys.stdout.write(json.dumps({"ok": True, "stage": "shutdown"}) + "\n")
            sys.stdout.flush()
            break
        if cmd.get("cmd") == "reauth":
            # Session refresh: re-read the credential file (FR-01 level 2).
            # No credentials are accepted through the command protocol.
            creds = read_credential_file()
            if creds is None:
                NEEDS_AUTH_MARKER.write_text("reauth: credential file unavailable\n")
                sys.stdout.write(json.dumps({
                    "ok": False, "error": "needs_auth: credential file unavailable"}) + "\n")
                sys.stdout.flush()
                continue
            try:
                authenticate(creds)
            except Exception as e:
                NEEDS_AUTH_MARKER.write_text("reauth failed\n")
                sys.stdout.write(json.dumps({"ok": False, "error": str(e)[:600]}) + "\n")
            else:
                sys.stdout.write(json.dumps({"ok": True, "stage": "reauthenticated"}) + "\n")
            sys.stdout.flush()
            continue
        fn = allowed.get(cmd.get("cmd"))
        if fn is None:
            out = {"ok": False, "error": "unknown or disallowed cmd"}
        else:
            params = {k: v for k, v in cmd.items() if k not in ("cmd", "save_to")}
            try:
                res = call_sync(fn, params)
                if cmd.get("cmd") in {"get_datasets", "get_datafields"} and isinstance(res, dict):
                    page_size = len(res.get("results") or [])
                    query = {
                        "instrumentType": params.get("instrument_type", "EQUITY"),
                        "region": params.get("region", "USA"),
                        "delay": params.get("delay", 1),
                        "universe": params.get("universe", "TOP3000"),
                        "limit": page_size,
                    }
                    if cmd.get("cmd") == "get_datafields":
                        if params.get("dataset_id"):
                            query["dataset.id"] = params["dataset_id"]
                        if params.get("data_type") and params["data_type"] != "ALL":
                            query["type"] = params["data_type"]
                    else:
                        query["theme"] = params.get("theme", "false")
                    if params.get("search"):
                        query["search"] = params["search"]
                    endpoint = ("data-fields" if cmd.get("cmd") == "get_datafields"
                                else "data-sets")
                    def next_url(offset):
                        return (f"{pf.brain_client.base_url}/{endpoint}?"
                                + urlencode({**query, "offset": offset}))
                    res = collect_pages(
                        res,
                        lambda url: pf.brain_client.session.get(url, timeout=40).json(),
                        pf.brain_client.base_url,
                        next_url_for_offset=next_url,
                    )
            except Exception as e:
                err = str(e)[:800]
                # If the session died mid-work, flag it so the supervisor
                # runs the FR-01 cascade instead of retrying blindly.
                if "401" in err or "unauthorized" in err.lower():
                    NEEDS_AUTH_MARKER.write_text("session expired during command\n")
                out = {"ok": False, "error": err}
            else:
                save_to = cmd.get("save_to")
                if save_to:
                    try:
                        Path(save_to).write_text(json.dumps(res, default=str), encoding="utf-8")
                        out = {"ok": True, "saved": save_to}
                    except Exception as e:
                        out = {"ok": False, "error": "save: " + str(e)[:200]}
                else:
                    out = {"ok": True, "result": res}
        try:
            sys.stdout.write(json.dumps(out, default=str) + "\n")
        except Exception as e:
            sys.stdout.write(json.dumps({"ok": False, "error": "serialize: " + str(e)[:200]}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
