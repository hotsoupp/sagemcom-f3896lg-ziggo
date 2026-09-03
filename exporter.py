#!/usr/bin/env python3
"""
Prometheus exporter for the Ziggo F3896LG cable modem.

Serves /metrics for Prometheus to scrape, reusing the fetch and unit
conversion logic in modem.py so the tenths quirks (OFDM/OFDMA power, OFDM
RxMER) only get applied in one place.

Every endpoint it reads is unauthenticated by design. It holds no credential
to store or leak, it can't reboot or reset the modem even if someone abused
it, and it never kicks out whoever's using the modem's web UI. The only
optional extra is the firmware version, read from the local firmware.log
that modem.py maintains.

The event log gets parsed for profile assignments, CM-STATUS events, reboots
and ranging faults. That log is a rolling window, older entries age out well
before the modem's uptime does, so those series are "_in_window" gauges
instead of counters. A counter built from a rolling log would look like it
keeps resetting.

Binds to localhost by default since /metrics is itself unauthenticated. Set
MODEM_EXPORTER_BIND=0.0.0.0 only if Prometheus scrapes from another host.

Usage:  python3 exporter.py                 serve on 127.0.0.1:9105
        python3 exporter.py --print         dump one scrape to stdout and exit
        MODEM_EXPORTER_BIND=0.0.0.0 MODEM_INTERVAL=60 python3 exporter.py
"""
import os
import re
import sys
import time
import threading
import collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import modem

PORT = int(os.environ.get("MODEM_EXPORTER_PORT", "9105"))
# /metrics carries no credentials but does expose modem state, so keep it
# local unless Prometheus genuinely lives elsewhere.
BIND = os.environ.get("MODEM_EXPORTER_BIND", "127.0.0.1")
# A poll costs whatever the modem decides, not what we do. It seems to refresh
# its DOCSIS stats on demand and cache them briefly, so the first request after
# that cache expires pays for the refresh and the rest of the poll is almost
# free. Measured medians for a full poll, about 0.2s when polling every 5s,
# 3.9s every 10s, 5.4s every 20s, 9s every 30s. The modem does the same work
# either way, a shorter interval just means more polls land on the cached path.
# Haven't gotten a clean median at 60s, the default here, yet. A rough check
# came back well under the 30s figure, but that run followed a lot of other
# polling against the same modem in the same session, so it is not trustworthy
# enough to quote.
#
# A couple things that didn't help, both measured. Fetching the endpoints
# concurrently was no better at a 30s cadence and threw a 28s outlier, since
# the cost is one refresh per poll and not per endpoint. Connection reuse
# didn't help either, TLS is 50ms either way. So we poll on a background
# thread and let /metrics serve the last snapshot instantly, which keeps
# scrapes fast no matter how slow a poll runs.
INTERVAL = float(os.environ.get("MODEM_INTERVAL", "60"))

# Event log message shapes. The upstream/downstream profile and reboot
# patterns follow ties/sagemcom-f3896-py. CM-STATUS is deliberately more
# permissive than theirs, since this firmware emits channel lists like
# "Chan ID: 4 12 31 32" and "Profile ID: N/A", which their stricter numeric
# pattern misses.
LOG_PATTERNS = [
    ("us_profile_change", re.compile(
        r"^US profile assignment change\. US Chan ID: (?P<channel_id>\d+); "
        r"Previous Profile: (?P<previous>[\d ]*); New Profile: (?P<profile>[\d ]+)\.")),
    ("ds_profile_change", re.compile(
        r"^DS profile assignment change\. DS Chan ID: (?P<channel_id>\d+); "
        r"Previous Profile: (?P<previous>[\d ]*); New Profile: (?P<profile>[\d ]+)\.")),
    ("cm_status", re.compile(
        r"^CM-STATUS message sent\. Event Type Code: (?P<event_code>\d+); "
        r"Chan ID: (?P<channel_id>[\d ]+|N/A);")),
    ("reboot", re.compile(r"^Cable Modem Reboot because of - (?P<reason>[^;]*)")),
    ("t3_timeout", re.compile(r"T3 time-?out")),
    ("t4_timeout", re.compile(r"T4 time-?out")),
    ("mdd_timeout", re.compile(r"^MDD message timeout")),
    ("gui_login", re.compile(r"^GUI Login Status")),
]

_lock = threading.Lock()
_refresh_lock = threading.Lock()
_cache = {"at": 0.0, "body": ""}
_last_success_at = 0.0


def parse_events(events):
    """Classify event log messages. Entries arrive newest-first."""
    counts, cm_status, reboots, profiles = (
        collections.Counter(), collections.Counter(), collections.Counter(), {})
    for e in events:
        msg = e.get("message") or ""
        for name, rx in LOG_PATTERNS:
            m = rx.search(msg)
            if not m:
                continue
            counts[name] += 1
            fields = m.groupdict()
            if name == "cm_status":
                cm_status[fields["event_code"]] += 1
            elif name.endswith("profile_change"):
                direction = "upstream" if name[0] == "u" else "downstream"
                # newest-first, so the first hit per channel is the current profile
                profiles.setdefault((direction, fields["channel_id"]),
                                    " ".join(fields["profile"].split()))
            elif name == "reboot":
                reboots[fields["reason"].strip()[:60]] += 1
            break
        else:
            counts["other"] += 1
    return counts, cm_status, reboots, profiles


def esc(value):
    """Escape a Prometheus label value."""
    return (str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))


def labels(pairs):
    inner = ",".join(f'{k}="{esc(v)}"' for k, v in pairs.items() if v is not None)
    return "{" + inner + "}" if inner else ""


def block(name, help_text, kind, samples):
    """One metric family. Samples with a None value are omitted, not zeroed."""
    rows = [f"{name}{labels(lb)} {v}" for lb, v in samples if v is not None]
    if not rows:
        return []
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"] + rows


def collect():
    global _last_success_at
    started = time.time()
    lines = []
    flows, localization, modemmode = [], {}, {}
    endpoint_status = {
        name: 0
        for name in (
            "downstream",
            "upstream",
            "state",
            "eventlog",
            "serviceflows",
            "localization",
            "modemmode",
        )
    }
    try:
        ds, us, cm, events = modem.fetch_snapshot(endpoint_status)
        # the exporter-only extras, unauthenticated as well
        flows = modem.get_optional("/rest/v1/cablemodem/serviceflows", "serviceFlows", [],
                                   endpoint_status, "serviceflows")
        localization = modem.get_optional("/rest/v1/system/localization", "localization", {},
                                          endpoint_status, "localization")
        modemmode = modem.get_optional("/rest/v1/system/modemmode", "modemmode", {},
                                       endpoint_status, "modemmode")
        up = 1
        _last_success_at = time.time()
    except Exception as exc:
        print(f"scrape failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        ds = us = events = []
        cm = {}
        up = 0

    def lb(ch):
        return {"channel_id": ch.get("channelId"), "channel_type": ch.get("channelType")}

    # --- device ---
    history = modem.read_firmware_log(modem.FIRMWARE_PATH)
    if cm or history or localization:
        lines += block("modem_info", "Static modem identity; value is always 1.", "gauge", [
            ({"model": localization.get("modelName"),
              "software_version": history[-1]["version"] if history else None,
              "docsis_version": cm.get("docsisVersion"),
              "status": cm.get("status")}, 1)])
    lines += block("modem_uptime_seconds", "Cable modem uptime.", "gauge",
                   [({}, cm.get("upTime"))])
    lines += block("modem_boot_time_seconds",
                   "Unix time the modem booted; stable across scrapes, unlike uptime.", "gauge",
                   [({}, round(time.time() - cm["upTime"]) if cm.get("upTime") else None)])
    lines += block("modem_bridge_mode",
                   "1 if the modem is in bridge mode (router stack disabled).", "gauge",
                   [({}, int(bool(modemmode.get("enable"))) if modemmode else None)])

    # --- provisioned service flows: the tier the CMTS has you on ---
    sf = [f.get("serviceFlow", {}) for f in flows]
    # serviceFlowId churns on re-registration, so it is deliberately not a label
    lines += block("modem_provisioned_rate_bps",
                   "Maximum traffic rate the CMTS has provisioned.", "gauge",
                   [({"direction": f.get("direction")}, f.get("maxTrafficRate"))
                    for f in sf if f.get("direction")])
    lines += block("modem_provisioned_max_burst_bytes",
                   "Maximum traffic burst the CMTS has provisioned.", "gauge",
                   [({"direction": f.get("direction")}, f.get("maxTrafficBurst"))
                    for f in sf if f.get("direction")])

    # --- downstream ---
    lines += block("modem_downstream_power_dbmv", "Downstream receive power.", "gauge",
                   [(lb(c), c["powerDbmv"]) for c in ds])
    lines += block("modem_downstream_snr_db", "Downstream SNR (SC-QAM only).", "gauge",
                   [(lb(c), c.get("snr")) for c in ds])
    lines += block("modem_downstream_rxmer_db", "Downstream RxMER.", "gauge",
                   [(lb(c), c["rxMerDb"]) for c in ds])
    lines += block("modem_downstream_frequency_hz", "Downstream centre frequency (SC-QAM only).",
                   "gauge", [(lb(c), c.get("frequency")) for c in ds])
    lines += block("modem_downstream_channel_width_hz", "Downstream channel width (OFDM only).",
                   "gauge", [(lb(c), c.get("channelWidth")) for c in ds])
    lines += block("modem_downstream_locked", "1 if the downstream channel is locked.", "gauge",
                   [(lb(c), int(bool(c.get("lockStatus")))) for c in ds])
    lines += block("modem_downstream_corrected_errors_total",
                   "Codewords corrected by FEC since boot.", "counter",
                   [(lb(c), c.get("correctedErrors")) for c in ds])
    lines += block("modem_downstream_uncorrected_errors_total",
                   "Codewords FEC could not correct since boot.", "counter",
                   [(lb(c), c.get("uncorrectedErrors")) for c in ds])

    # --- upstream ---
    lines += block("modem_upstream_power_dbmv", "Upstream transmit power.", "gauge",
                   [(lb(c), c["powerDbmv"]) for c in us])
    lines += block("modem_upstream_frequency_hz", "Upstream centre frequency (ATDMA only).",
                   "gauge", [(lb(c), c.get("frequency")) for c in us])
    lines += block("modem_upstream_channel_width_hz", "Upstream channel width (OFDMA only).",
                   "gauge", [(lb(c), c.get("channelWidth")) for c in us])
    lines += block("modem_upstream_locked", "1 if the upstream channel is locked.", "gauge",
                   [(lb(c), int(bool(c.get("lockStatus")))) for c in us])
    lines += block("modem_upstream_symbol_rate_ksps",
                   "Upstream symbol rate as reported by the modem (ATDMA only).", "gauge",
                   [(lb(c), c.get("symbolRate")) for c in us])
    lines += block("modem_upstream_ranging_timeouts_total",
                   "DOCSIS ranging timeouts since boot, by timer.", "counter",
                   [(dict(lb(c), timer=t), c.get(f"{t}Timeout"))
                    for c in us for t in ("t1", "t2", "t3", "t4")])

    # modulation moves when the CMTS reassigns a profile, so keep it out of the
    # numeric series' labels and expose it on its own to avoid series churn
    lines += block("modem_channel_modulation_info", "Current modulation; value is always 1.",
                   "gauge", [(dict(lb(c), direction=d, modulation=c.get("modulation")), 1)
                             for d, chans in (("downstream", ds), ("upstream", us))
                             for c in chans if c.get("modulation")])

    # --- event log ---
    if events:
        by_priority = collections.Counter(e.get("priority") or "unknown" for e in events)
        lines += block("modem_event_log_entries", "Entries currently held in the modem event log.",
                       "gauge", [({"priority": p}, n) for p, n in sorted(by_priority.items())])

        counts, cm_status, reboots, profiles = parse_events(events)
        lines += block("modem_log_events_in_window",
                       "Parsed event log messages currently in the rolling log window.", "gauge",
                       [({"type": t}, n) for t, n in sorted(counts.items())])
        lines += block("modem_cm_status_events_in_window",
                       "CM-STATUS messages in the log window, by DOCSIS event type code "
                   "(4=MDD recovery, 20=NCP profile failure, 21=PLC failure, "
                   "22=NCP profile recovery).", "gauge",
                       [({"event_code": c}, n) for c, n in sorted(cm_status.items())])
        lines += block("modem_reboot_events_in_window",
                       "Logged modem reboots in the log window, by reason.", "gauge",
                       [({"reason": r}, n) for r, n in sorted(reboots.items())])
        lines += block("modem_channel_profile_info",
                       "Most recent profile assigned to a channel; value is always 1.", "gauge",
                       [({"direction": d, "channel_id": c, "profile": pr}, 1)
                        for (d, c), pr in sorted(profiles.items())])

    lines += block("modem_up", "1 if the last scrape of the modem succeeded.", "gauge", [({}, up)])
    lines += block("modem_endpoint_up", "1 if the endpoint answered in the last poll.", "gauge",
                   [({"endpoint": name}, success) for name, success in sorted(endpoint_status.items())])
    lines += block("modem_scrape_duration_seconds", "Duration of the last modem poll.", "gauge",
                   [({}, round(time.time() - started, 3))])
    lines += block("modem_last_poll_timestamp_seconds",
                   "Unix time of the last modem poll; alert if this goes stale.", "gauge",
                   [({}, round(time.time(), 3))])
    lines += block("modem_last_success_timestamp_seconds",
                   "Unix time of the last successful required-endpoint poll.", "gauge",
                   [({}, round(_last_success_at, 3))] if _last_success_at else [])
    return "\n".join(lines) + "\n"


def refresh():
    with _refresh_lock:
        body = collect()
        with _lock:
            _cache["body"] = body
            _cache["at"] = time.time()


def poller():
    """Refresh the cache every INTERVAL seconds, measured start to start.

    Sleeping INTERVAL between polls would make the real period INTERVAL plus
    however long the modem took to answer, which on a slow link drifts well
    past the Prometheus scrape interval. Consecutive scrapes then read the same
    cached snapshot, and a rate() over two identical samples is zero, so the
    graphs sawtooth. Holding the cadence keeps every scrape on fresh values.
    """
    next_at = time.monotonic() + INTERVAL
    while True:
        time.sleep(max(0.0, next_at - time.monotonic()))
        try:
            refresh()
        except Exception as exc:                      # keep the thread alive
            print(f"poll error: {type(exc).__name__}: {exc}", file=sys.stderr)
        next_at += INTERVAL
        if next_at <= time.monotonic():               # fell behind, do not burst
            next_at = time.monotonic() + INTERVAL


def cached():
    with _lock:
        body = _cache["body"]
    if body:
        return body
    return "\n".join([
        "# HELP modem_up 1 if the last scrape of the modem succeeded.",
        "# TYPE modem_up gauge",
        "modem_up 0",
        "",
    ])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        body = cached().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # don't spam stdout on every scrape


def main():
    if "--print" in sys.argv:
        sys.stdout.write(collect())
        return
    threading.Thread(target=refresh, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    print(f"modem exporter listening on {BIND}:{PORT}/metrics and /healthz "
          f"(polling the modem every {INTERVAL:g}s, no credentials used)")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
