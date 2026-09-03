#!/usr/bin/env python3
"""
Fetch modem stats, print a readable summary, and write stats.json as a local
record, used to carry the firmware version forward between runs and there if
you want to script against the data yourself.

Every endpoint except /rest/v1/system/info works without logging in, so this
only authenticates when MODEM_PASSWORD is set. That matters because the modem
only allows one session at a time and answers a 503 to a second login, so
logging in when you don't need to is what locks you out of the web UI (and the
other way around too). When it does log in, it always hands the token back.

/rest/v1/system/info is just the firmware version. It gets read on every run
that has a password, and each new value gets appended to firmware.log. Ziggo
pushes firmware silently, so a dated record helps when a line goes bad and you
want to know if an update lines up with it. If the web UI already has the
session, the last known version is reused instead of kicking anyone out.

exporter.py imports this file for the fetching and unit conversion, so the
tenths quirks only live in one place.

Usage:  python3 modem.py                       no login, reuses last known firmware
        MODEM_PASSWORD='...' python3 modem.py  reads firmware, logs any change
"""
import os
import re
import json
import time
import datetime
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HOST = os.environ.get("MODEM_HOST", "https://192.168.100.1")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATS_PATH = os.path.join(BASE_DIR, "stats.json")
FIRMWARE_PATH = os.path.join(BASE_DIR, "firmware.log")

# OFDM/OFDMA report power in tenths, OFDM also reports RxMER in tenths, real
# units everywhere else. Checked this against the modem's own web UI.
TENTHS_POWER = {"ofdm", "ofdma"}
TENTHS_RXMER = {"ofdm"}

# Event messages carry a ;CM-MAC=..;CMTS-MAC=..; trailer, and stats.json gets
# served over HTTP, so mask them like the serial number and state MAC.
MAC_RE = re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b")

# Retry a login 503 briefly, it might just be a stale session closing, then
# give up. Never evict whoever's actually logged in, see login() below.
LOGIN_RETRIES = 2
LOGIN_BACKOFF = 3
REQUEST_TIMEOUT = float(os.environ.get("MODEM_REQUEST_TIMEOUT", "10"))
REQUEST_RETRIES = int(os.environ.get("MODEM_REQUEST_RETRIES", "3"))
REQUEST_BACKOFF = float(os.environ.get("MODEM_REQUEST_BACKOFF", "1"))

if REQUEST_TIMEOUT <= 0:
    raise ValueError("MODEM_REQUEST_TIMEOUT must be greater than zero")
if REQUEST_RETRIES < 1:
    raise ValueError("MODEM_REQUEST_RETRIES must be at least one")
if REQUEST_BACKOFF < 0:
    raise ValueError("MODEM_REQUEST_BACKOFF must not be negative")

# Ziggo's published good/tolerated signal values, same ones the Grafana
# dashboard draws as threshold lines. Downstream rated by distance from 0,
# -7..+7 good, out to +/-11 tolerated. Upstream ranges differ by type since
# OFDMA is measured across a wider block and just runs lower than ATDMA, so
# one range can't cover both.
DS_POWER_GOOD = 7.0
DS_POWER_TOLERATED = 11.0
DS_SNR_MIN = 33.0
US_POWER_RANGE = {"atdma": (35.0, 49.0), "ofdma": (30.0, 45.0)}
US_POWER_DEFAULT = (35.0, 51.0)

# Reuse one session for every request, so TLS only shakes hands once per run
# instead of once per endpoint.
_session = requests.Session()
_session.verify = False


def us_range(ch):
    return US_POWER_RANGE.get(ch.get("channelType"), US_POWER_DEFAULT)


def norm_power(ch):
    """OFDM/OFDMA report power in tenths of dBmV, SC-QAM/ATDMA are real dBmV."""
    p = ch.get("power")
    if p is None:
        return None
    return p / 10.0 if ch.get("channelType") in TENTHS_POWER else p


def norm_rxmer(ch):
    """OFDM reports RxMER in tenths of dB, and a flat 0 there means 'not reported'."""
    v = ch.get("rxMer")
    if v is None:
        return None
    if ch.get("channelType") in TENTHS_RXMER:
        return (v / 10.0) or None
    return v


def mhz(value):
    return None if value is None else round(value / 1e6, 1)


def get(path, headers=None, attempts=REQUEST_RETRIES):
    """GET with retries and capped exponential backoff."""
    last = None
    for attempt in range(attempts):
        try:
            r = _session.get(f"{HOST}{path}", headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(REQUEST_BACKOFF * min(2 ** attempt, 4))
    raise last


def get_optional(path, key, default, endpoint_status=None, endpoint=None):
    """GET for the endpoints the tools can live without."""
    try:
        result = get(path).get(key) or default
    except (requests.RequestException, ValueError):
        if endpoint_status is not None:
            endpoint_status[endpoint or path] = 0
        return default
    if endpoint_status is not None:
        endpoint_status[endpoint or path] = 1
    return result


def fetch_snapshot(endpoint_status=None):
    """Fetch and normalise everything both the summary and the exporter read.

    Downstream and upstream are required and raise if unreachable. Modem state
    and the event log just fall back to empty. Event messages get their MAC
    trailers masked right here so no caller can forget to, and channels get
    powerDbmv, rxMerDb and MHz fields added so nothing downstream needs to know
    about the tenths quirk or which channel types skip a frequency.
    """
    try:
        ds = get("/rest/v1/cablemodem/downstream").get("downstream", {}).get("channels", [])
        if endpoint_status is not None:
            endpoint_status["downstream"] = 1
    except (requests.RequestException, ValueError):
        if endpoint_status is not None:
            endpoint_status["downstream"] = 0
        raise
    try:
        us = get("/rest/v1/cablemodem/upstream").get("upstream", {}).get("channels", [])
        if endpoint_status is not None:
            endpoint_status["upstream"] = 1
    except (requests.RequestException, ValueError):
        if endpoint_status is not None:
            endpoint_status["upstream"] = 0
        raise
    cm = get_optional("/rest/v1/cablemodem/state_", "cablemodem", {},
                      endpoint_status, "state")
    events = get_optional("/rest/v1/cablemodem/eventlog", "eventlog", [],
                          endpoint_status, "eventlog")
    events = [dict(e, message=MAC_RE.sub("<mac>", e.get("message") or "")) for e in events]
    for ch in ds + us:
        ch["powerDbmv"] = norm_power(ch)
        ch["frequencyMhz"] = mhz(ch.get("frequency"))
        ch["channelWidthMhz"] = mhz(ch.get("channelWidth"))
    for ch in ds:
        ch["rxMerDb"] = norm_rxmer(ch)
    return ds, us, cm, events


def login(password, retries):
    """Return (token, userId), or None if the modem won't hand out a session.

    A 503 means the single session is already taken, most often by a browser
    sitting on the modem's web UI. That session is someone's, so wait a moment
    in case it's just a stale one closing, then give up instead of evicting it.
    """
    for attempt in range(1, retries + 1):
        r = _session.post(f"{HOST}/rest/v1/user/login",
                          json={"password": password}, timeout=REQUEST_TIMEOUT)
        if r.status_code == 201:
            created = r.json().get("created", {})
            return created.get("token"), created.get("userId")
        if r.status_code != 503:
            print(f"  note: login failed ({r.status_code})")
            return None
        if attempt < retries:
            time.sleep(LOGIN_BACKOFF)
    print("  note: someone is logged into the modem web UI - leaving that session alone")
    return None


def fetch_info(password, retries):
    """Read /rest/v1/system/info, the one endpoint that needs authentication.

    Login hands back a bearer token in the body. There's no cookie, so it goes
    out as an Authorization header instead. The token always gets released
    again via DELETE /rest/v1/user/<id>/token/<token>, same call the web UI's
    logout button makes (answers 204).
    """
    session = login(password, retries)
    if not session:
        return {}
    token, user_id = session
    headers = {"Authorization": f"Bearer {token}"}
    try:
        return get("/rest/v1/system/info", headers).get("info", {})
    except Exception:
        print("  note: could not read /rest/v1/system/info")
        return {}
    finally:
        # release the single-session lock even if the read above failed
        try:
            d = _session.delete(f"{HOST}/rest/v1/user/{user_id}/token/{token}",
                                headers=headers, timeout=REQUEST_TIMEOUT)
            if d.status_code != 204:
                print(f"  note: logout returned {d.status_code}; modem may stay locked briefly")
        except Exception:
            print("  note: logout failed; modem may stay locked briefly")


def load_previous(path):
    """Last run's stats.json, used to carry device details forward on a 503."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def read_firmware_log(path):
    """Parse firmware.log into [{firstSeen, version}], oldest first."""
    entries = []
    try:
        with open(path) as f:
            for line in f:
                stamp, _, version = line.strip().partition("\t")
                if version:
                    entries.append({"firstSeen": stamp, "version": version})
    except OSError:
        # missing file, or a directory left by an empty bind mount, either way
        # the version's optional so don't crash over it
        pass
    return entries


def record_firmware(path, version):
    """Append version to firmware.log when it differs from the last one seen."""
    entries = read_firmware_log(path)
    if not version or (entries and entries[-1]["version"] == version):
        return entries
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with open(path, "a") as f:
        f.write(f"{stamp}\t{version}\n")
    if entries:
        print(f"  FIRMWARE CHANGED: {entries[-1]['version']} -> {version}")
    else:
        print(f"  firmware recorded: {version}")
    entries.append({"firstSeen": stamp, "version": version})
    return entries


def device_info(password):
    """Resolve firmware details, live from the modem when possible.

    Without a password, while the web UI holds the session, or if the login
    just fails, falls back to whatever's in the previous stats.json.
    """
    try:
        info = fetch_info(password, LOGIN_RETRIES) if password else {}
    except Exception:
        # login has no retry, and the channel data's already fetched by now,
        # so don't lose the whole run over the optional firmware version
        print("  note: could not reach the modem for the firmware version")
        info = {}
    checked = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if not info.get("softwareVersion"):
        previous = load_previous(STATS_PATH).get("device") or {}
        info = {k: previous.get(k) for k in ("modelName", "softwareVersion", "hardwareVersion")}
        checked = previous.get("firmwareCheckedAt")
        if not password:
            print("  note: set MODEM_PASSWORD to read and track the firmware version")
    history = record_firmware(FIRMWARE_PATH, info.get("softwareVersion"))
    return info, checked, history


def fmt_uptime(seconds):
    if not seconds:
        return None
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    return f"{days}d {hours}h {rem // 60}m"


def num(v, d=1):
    return "-" if v is None else f"{v:.{d}f}"


def count(v):
    return "-" if v is None else f"{v:,}"


def span(ch):
    """SC-QAM/ATDMA report a centre frequency, OFDM/OFDMA only report a width."""
    if ch["frequencyMhz"] is not None:
        return f"{ch['frequencyMhz']:.1f}"
    if ch["channelWidthMhz"] is not None:
        return f"{ch['channelWidthMhz']:.1f}w"
    return "-"


def print_downstream(ds_channels):
    print("\n=== DOWNSTREAM ===")
    print(f"{'ID':>4} {'Type':<7} {'MHz':>9} {'Power':>7} {'SNR':>5} "
          f"{'RxMER':>6} {'Corrected':>14} {'Uncorr':>8} {'Lock':>5}")
    for ch in ds_channels:
        print(f"{str(ch.get('channelId')):>4} {ch.get('channelType',''):<7} "
              f"{span(ch):>9} {num(ch['powerDbmv']):>7} {num(ch.get('snr'), 0):>5} "
              f"{num(ch['rxMerDb']):>6} {count(ch.get('correctedErrors')):>14} "
              f"{count(ch.get('uncorrectedErrors')):>8} "
              f"{'yes' if ch.get('lockStatus') else 'NO':>5}")


def print_upstream(us_channels):
    print("\n=== UPSTREAM ===")
    print(f"{'ID':>4} {'Type':<7} {'MHz':>9} {'Power':>7} {'Modulation':<11} "
          f"{'T3':>4} {'T4':>4} {'Lock':>5}")
    for ch in us_channels:
        print(f"{str(ch.get('channelId')):>4} {ch.get('channelType',''):<7} "
              f"{span(ch):>9} {num(ch['powerDbmv']):>7} {ch.get('modulation',''):<11} "
              f"{str(ch.get('t3Timeout','-')):>4} {str(ch.get('t4Timeout','-')):>4} "
              f"{'yes' if ch.get('lockStatus') else 'NO':>5}")
    print("  (a 'w' after the MHz column means channel width - OFDM/OFDMA report"
          " no centre frequency)")


def print_notes(ds_channels, us_channels, events):
    print("\n=== NOTES ===")
    clean = True
    bad = [c.get("channelId") for c in ds_channels
           if c["powerDbmv"] is not None and abs(c["powerDbmv"]) > DS_POWER_TOLERATED]
    if bad:
        print(f"  Downstream power outside +/-{DS_POWER_TOLERATED:g} dBmV: {bad}")
        clean = False
    marginal = [c.get("channelId") for c in ds_channels
                if c["powerDbmv"] is not None
                and DS_POWER_GOOD < abs(c["powerDbmv"]) <= DS_POWER_TOLERATED]
    if marginal:
        print(f"  Downstream power outside the ideal +/-{DS_POWER_GOOD:g} dBmV"
              f" but still tolerated: {marginal}")
    low_snr = [c.get("channelId") for c in ds_channels
               if c.get("snr") is not None and c["snr"] < DS_SNR_MIN]
    if low_snr:
        print(f"  Downstream SNR below {DS_SNR_MIN:g} dB: {low_snr}")
        clean = False
    out_of_range = [(c.get("channelId"), c["powerDbmv"], us_range(c)) for c in us_channels
                    if c["powerDbmv"] is not None
                    and not us_range(c)[0] <= c["powerDbmv"] <= us_range(c)[1]]
    if out_of_range:
        for cid, power, (lo, hi) in out_of_range:
            print(f"  Upstream ch{cid} at {power:.1f} dBmV is outside {lo:g}-{hi:g}")
        clean = False
    unlocked = [c.get("channelId") for c in ds_channels + us_channels if not c.get("lockStatus")]
    if unlocked:
        print(f"  Unlocked channels: {unlocked}")
        clean = False
    timeouts = [(c.get("channelId"), c.get("t3Timeout") or 0, c.get("t4Timeout") or 0)
                for c in us_channels if c.get("t3Timeout") or c.get("t4Timeout")]
    if timeouts:
        print("  Upstream ranging timeouts: "
              + ", ".join(f"ch{i} T3={t3} T4={t4}" for i, t3, t4 in timeouts))
        clean = False
    notable = [e for e in events if e.get("priority") not in ("notice", None)]
    if notable:
        print(f"  Event log holds {len(notable)} non-notice entries; most recent:")
        for e in notable[:5]:
            print(f"    [{e.get('priority')}] {e.get('time')} {e.get('message','')[:78]}")
        clean = False
    if clean:
        print("  All channels locked and within Ziggo's ranges; no ranging timeouts.")


def main():
    ds_channels, us_channels, cm, events = fetch_snapshot()
    password = os.environ.get("MODEM_PASSWORD")
    info, checked, history = device_info(password)

    print_downstream(ds_channels)
    print_upstream(us_channels)
    print_notes(ds_channels, us_channels, events)

    # --- write stats.json ---
    out = {
        "generatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "device": {
            "modelName": info.get("modelName"),
            "softwareVersion": info.get("softwareVersion"),
            "hardwareVersion": info.get("hardwareVersion"),
            "docsisVersion": cm.get("docsisVersion"),
            "status": cm.get("status"),
            "upTimeSeconds": cm.get("upTime"),
            "uptime": fmt_uptime(cm.get("upTime")),
            "firmwareCheckedAt": checked,
        },
        "firmwareHistory": history,
        "downstream": ds_channels,
        "upstream": us_channels,
        "eventlog": events,
    }
    # serial number and MAC addresses are deliberately left out, nothing here
    # needs them and there's no reason to keep them on disk

    with open(STATS_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {STATS_PATH}")


if __name__ == "__main__":
    main()
