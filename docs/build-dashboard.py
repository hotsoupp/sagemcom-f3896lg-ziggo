#!/usr/bin/env python3
"""Generate grafana-dashboard.json for the sagemcom-f3896lg-ziggo exporter.

Design notes.

Per-channel lines do not work for downstream. 32 SC-QAM channels plus 2 OFDM
means a cycled palette, repeated colours and a legend taller than the plot. The
question those panels answer is "is any channel drifting out of Ziggo's range",
which is min/avg/max, three series. Per-channel detail lives in a table below,
where 34 rows is a feature rather than spaghetti.

Flat-zero series (unlocked channels, ranging timeouts) need a soft max or
Grafana scales the axis 0..100 and a healthy modem looks like an empty chart.
"""
import os
import json

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
_id = 0

# Ziggo's published ranges, the same numbers modem.py checks against.
DS_POWER_STEPS = [("red", None), ("yellow", -11.0), ("green", -7.0),
                  ("yellow", 7.0), ("red", 11.0)]
DS_SNR_STEPS = [("red", None), ("green", 33.0)]
ATDMA_POWER_STEPS = [("yellow", None), ("green", 35.0), ("yellow", 49.0),
                     ("red", 51.0)]
# OFDMA is measured across a wider block and sits legitimately lower, so it
# needs its own bands. Sharing one panel with ATDMA drew the 35 dBmV line under
# healthy OFDMA channels and made them look out of range.
OFDMA_POWER_STEPS = [("red", None), ("yellow", 28.0), ("green", 30.0),
                     ("yellow", 45.0)]
UNCORR_STEPS = [("green", None), ("yellow", 0.01), ("red", 1.0)]
T3_STEPS = [("green", None), ("yellow", 1.0)]
T4_STEPS = [("green", None), ("red", 1.0)]
LOCK_MAP = [{"type": "value", "options": {
    "1": {"text": "yes", "color": "green"},
    "0": {"text": "NO", "color": "red"}}}]


def nid():
    global _id
    _id += 1
    return _id


def steps(pairs):
    return {"mode": "absolute",
            "steps": [{"color": c, "value": v} for c, v in pairs]}


def target(expr, legend=None, instant=False, table=False):
    t = {"datasource": DS, "expr": expr}
    if legend:
        t["legendFormat"] = legend
    if instant:
        t["instant"] = True
        t["range"] = False
    if table:
        t["format"] = "table"
    return t


def targets(*ts):
    for i, t in enumerate(ts):
        t["refId"] = chr(ord("A") + i)
    return list(ts)


def timeseries(title, gp, ts, unit="short", axis=None, desc=None, draw="line",
               stack=False, fill=0, step=False, overrides=None, tsteps=None,
               tstyle="off", soft=None, width=1):
    p = {
        "id": nid(), "type": "timeseries", "title": title, "gridPos": gp,
        "datasource": DS, "targets": ts,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "unit": unit,
                "custom": {
                    "drawStyle": draw, "lineWidth": width, "fillOpacity": fill,
                    "pointSize": 5, "showPoints": "never", "spanNulls": True,
                    "lineInterpolation": "stepAfter" if step else "linear",
                    "stacking": {"mode": "normal" if stack else "none"},
                    "thresholdsStyle": {"mode": tstyle},
                },
                "thresholds": steps(tsteps or [("green", None)]),
            },
            "overrides": overrides or [],
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {"displayMode": "list", "placement": "bottom",
                       "showLegend": True, "calcs": []},
        },
    }
    if axis:
        p["fieldConfig"]["defaults"]["custom"]["axisLabel"] = axis
    if soft:
        lo, hi = soft
        if lo is not None:
            p["fieldConfig"]["defaults"]["custom"]["axisSoftMin"] = lo
        if hi is not None:
            p["fieldConfig"]["defaults"]["custom"]["axisSoftMax"] = hi
    if desc:
        p["description"] = desc
    return p


def stat(title, gp, ts, unit="short", mappings=None, tsteps=None, desc=None,
         decimals=None):
    p = {
        "id": nid(), "type": "stat", "title": title, "gridPos": gp,
        "datasource": DS, "targets": ts,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "unit": unit,
                "mappings": mappings or [],
                "thresholds": steps(tsteps or [("text", None)]),
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value", "graphMode": "none", "justifyMode": "auto",
            "textMode": "auto",
        },
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    if desc:
        p["description"] = desc
    return p


def col(name, tsteps=None, mappings=None, unit=None, decimals=None, color=True):
    """One coloured column in a table panel."""
    props = []
    if color:
        props.append({"id": "custom.cellOptions", "value": {"type": "color-text"}})
    if tsteps:
        props.append({"id": "thresholds", "value": steps(tsteps)})
    if mappings:
        props.append({"id": "mappings", "value": mappings})
    if unit:
        props.append({"id": "unit", "value": unit})
    if decimals is not None:
        props.append({"id": "decimals", "value": decimals})
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def table(title, gp, ts, transformations, overrides=None, desc=None):
    p = {
        "id": nid(), "type": "table", "title": title, "gridPos": gp,
        "datasource": DS, "targets": ts,
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "auto", "filterable": False,
                           "cellOptions": {"type": "auto"}},
                "color": {"mode": "thresholds"},
                "thresholds": steps([("text", None)]),
            },
            "overrides": overrides or [],
        },
        "options": {"showHeader": True, "footer": {"show": False}},
        "transformations": transformations,
    }
    if desc:
        p["description"] = desc
    return p


def organize(exclude=(), rename=None, order=None):
    return {"id": "organize", "options": {
        "excludeByName": {c: True for c in exclude},
        "renameByName": rename or {},
        "indexByName": order or {}}}


def row(title, y):
    return {"id": nid(), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}


RATE = "5m"

panels = [
    # ------------------------------------------------------------------ status
    row("Status", 0),
    stat("Modem", {"h": 4, "w": 3, "x": 0, "y": 1},
         targets(target("modem_up")),
         mappings=[{"type": "value", "options": {
             "1": {"text": "UP", "color": "green"},
             "0": {"text": "DOWN", "color": "red"}}}]),
    stat("Unlocked channels", {"h": 4, "w": 3, "x": 3, "y": 1},
         targets(target("(count(modem_downstream_locked == 0) or vector(0))"
                        " + (count(modem_upstream_locked == 0) or vector(0))")),
         tsteps=[("green", None), ("red", 1.0)],
         desc="Downstream and upstream channels reporting unlocked. "
              "Should be zero."),
    stat("Lowest RxMER", {"h": 4, "w": 3, "x": 6, "y": 1},
         targets(target("min(modem_downstream_rxmer_db)")),
         unit="dB", decimals=1, tsteps=DS_SNR_STEPS,
         desc="Worst channel right now. Ziggo rates above 33 dB as good, so "
              "this going red means at least one channel is struggling."),
    stat("Ranging timeouts, 1h", {"h": 4, "w": 3, "x": 9, "y": 1},
         targets(target("sum(increase(modem_upstream_ranging_timeouts_total[1h]))")),
         decimals=0, tsteps=[("green", None), ("yellow", 1.0), ("red", 10.0)],
         desc="New T1 to T4 timeouts across all upstream channels in the last "
              "hour. Anything above zero means the upstream had trouble."),
    stat("Uptime", {"h": 4, "w": 3, "x": 12, "y": 1},
         targets(target("modem_uptime_seconds")), unit="s"),
    stat("Mode", {"h": 4, "w": 3, "x": 15, "y": 1},
         targets(target("modem_bridge_mode")),
         mappings=[{"type": "value", "options": {
             "1": {"text": "Bridge", "color": "blue"},
             "0": {"text": "Router", "color": "purple"}}}]),
    stat("Provisioned down", {"h": 4, "w": 3, "x": 18, "y": 1},
         targets(target('modem_provisioned_rate_bps{direction="downstream"}')),
         unit="bps", desc="Maximum rate the CMTS has provisioned."),
    stat("Provisioned up", {"h": 4, "w": 3, "x": 21, "y": 1},
         targets(target('modem_provisioned_rate_bps{direction="upstream"}')),
         unit="bps"),
    table("Device", {"h": 4, "w": 24, "x": 0, "y": 5},
          targets(target("modem_info", instant=True, table=True)),
          [organize(exclude=["Time", "Value", "__name__", "instance", "job"],
                    rename={"model": "Model", "software_version": "Firmware",
                            "docsis_version": "DOCSIS", "status": "Status"},
                    order={"model": 0, "software_version": 1,
                           "docsis_version": 2, "status": 3})],
          desc="Firmware appears once modem.py has run with MODEM_PASSWORD set, "
               "which is what fills firmware.log."),

    # -------------------------------------------------------------- downstream
    row("Downstream", 9),
    timeseries("Downstream power, across all channels",
               {"h": 8, "w": 12, "x": 0, "y": 10},
               targets(target("min(modem_downstream_power_dbmv)", "min"),
                       target("avg(modem_downstream_power_dbmv)", "avg"),
                       target("max(modem_downstream_power_dbmv)", "max")),
               axis="dBmV", soft=(-12, 12), tsteps=DS_POWER_STEPS,
               tstyle="dashed",
               overrides=[
                   {"matcher": {"id": "byName", "options": "avg"},
                    "properties": [{"id": "custom.lineWidth", "value": 2}]}],
               desc="Ziggo rates -7 to +7 dBmV as good, out to +/-11 tolerated. "
                    "The dashed lines mark those bands. Per-channel values are "
                    "in the table below."),
    timeseries("Downstream RxMER, across all channels",
               {"h": 8, "w": 12, "x": 12, "y": 10},
               targets(target("min(modem_downstream_rxmer_db)", "min"),
                       target("avg(modem_downstream_rxmer_db)", "avg"),
                       target("max(modem_downstream_rxmer_db)", "max")),
               axis="dB", soft=(28, 45), tsteps=DS_SNR_STEPS, tstyle="dashed",
               overrides=[
                   {"matcher": {"id": "byName", "options": "avg"},
                    "properties": [{"id": "custom.lineWidth", "value": 2}]}],
               desc="Ziggo rates above 33 dB as good. SNR tracks RxMER closely "
                    "on this modem and is in the table below."),
    timeseries("FEC corrected", {"h": 7, "w": 12, "x": 0, "y": 18},
               targets(target(f"sum by (channel_type) (rate(modem_downstream_corrected_errors_total[{RATE}]))",
                              "{{channel_type}}")),
               axis="codewords / s",
               desc="Codewords FEC repaired. Normal, and much higher on OFDM "
                    "because it carries far more codewords."),
    timeseries("FEC uncorrected", {"h": 7, "w": 12, "x": 12, "y": 18},
               targets(target(f"sum by (channel_type) (rate(modem_downstream_uncorrected_errors_total[{RATE}]))",
                              "{{channel_type}}")),
               axis="codewords / s", soft=(0, 1), tsteps=UNCORR_STEPS,
               desc="Codewords FEC could not repair, which is real packet loss. "
                    "On its own scale because corrected dwarfs it."),
    table("Downstream channels", {"h": 17, "w": 24, "x": 0, "y": 25},
          targets(
              target("sum by (channel_id, channel_type) (modem_downstream_power_dbmv)",
                     instant=True, table=True),
              target("sum by (channel_id) (modem_downstream_snr_db)",
                     instant=True, table=True),
              target("sum by (channel_id) (modem_downstream_rxmer_db)",
                     instant=True, table=True),
              target(f"sum by (channel_id) (rate(modem_downstream_corrected_errors_total[{RATE}]))",
                     instant=True, table=True),
              target(f"sum by (channel_id) (rate(modem_downstream_uncorrected_errors_total[{RATE}]))",
                     instant=True, table=True),
              target("sum by (channel_id) (modem_downstream_locked)",
                     instant=True, table=True)),
          [
              {"id": "joinByField",
               "options": {"byField": "channel_id", "mode": "outer"}},
              {"id": "convertFieldType",
               "options": {"conversions": [
                   {"targetField": "channel_id", "destinationType": "number"}]}},
              {"id": "sortBy",
               "options": {"sort": [{"field": "channel_id", "desc": False}]}},
              # joining six frames leaves one Time column per query
              organize(exclude=["Time"] + [f"Time {i}" for i in range(1, 6)],
                       rename={"channel_id": "Channel", "channel_type": "Type",
                               "Value #A": "Power dBmV", "Value #B": "SNR dB",
                               "Value #C": "RxMER dB",
                               "Value #D": "Corrected /s",
                               "Value #E": "Uncorrected /s",
                               "Value #F": "Locked"}),
          ],
          overrides=[
              col("Power dBmV", tsteps=DS_POWER_STEPS, decimals=1),
              col("SNR dB", tsteps=DS_SNR_STEPS, decimals=0),
              col("RxMER dB", tsteps=DS_SNR_STEPS, decimals=1),
              col("Uncorrected /s", tsteps=UNCORR_STEPS, decimals=2),
              col("Corrected /s", decimals=1, color=False),
              col("Locked", mappings=LOCK_MAP),
          ],
          desc="Every downstream channel with its current values, coloured "
               "against Ziggo's ranges. This is where you find which channel "
               "the summary charts are complaining about."),

    # ---------------------------------------------------------------- upstream
    row("Upstream", 42),
    timeseries("Upstream power, ATDMA", {"h": 8, "w": 8, "x": 0, "y": 43},
               targets(target('modem_upstream_power_dbmv{channel_type="atdma"}',
                              "ch {{channel_id}}")),
               axis="dBmV", soft=(33, 52), tsteps=ATDMA_POWER_STEPS,
               tstyle="dashed",
               desc="Ziggo rates 35 to 49 dBmV as good, up to 51 tolerated."),
    timeseries("Upstream power, OFDMA", {"h": 8, "w": 8, "x": 8, "y": 43},
               targets(target('modem_upstream_power_dbmv{channel_type="ofdma"}',
                              "ch {{channel_id}}")),
               axis="dBmV", soft=(28, 48), tsteps=OFDMA_POWER_STEPS,
               tstyle="dashed",
               desc="Ziggo rates 30 to 45 dBmV as good. OFDMA sits lower than "
                    "ATDMA on purpose, because it is measured across a wider "
                    "block of spectrum. That is normal and not a fault."),
    timeseries("Ranging timeouts", {"h": 8, "w": 8, "x": 16, "y": 43},
               targets(target(f"sum by (timer) (increase(modem_upstream_ranging_timeouts_total[{RATE}]))",
                              "{{timer}}")),
               draw="bars", stack=True, fill=60, soft=(0, 2),
               desc="New DOCSIS ranging timeouts per interval. T3 bursts mean "
                    "the upstream is struggling, T4 is worse."),

    table("Upstream channels", {"h": 9, "w": 24, "x": 0, "y": 51},
          targets(
              target("sum by (channel_id, channel_type) (modem_upstream_power_dbmv)",
                     instant=True, table=True),
              target('sum by (channel_id, modulation) (modem_channel_modulation_info{direction="upstream"})',
                     instant=True, table=True),
              target("sum by (channel_id) (modem_upstream_frequency_hz)",
                     instant=True, table=True),
              target("sum by (channel_id) (modem_upstream_channel_width_hz)",
                     instant=True, table=True),
              target('sum by (channel_id) (modem_upstream_ranging_timeouts_total{timer="t3"})',
                     instant=True, table=True),
              target('sum by (channel_id) (modem_upstream_ranging_timeouts_total{timer="t4"})',
                     instant=True, table=True),
              target("sum by (channel_id) (modem_upstream_locked)",
                     instant=True, table=True)),
          [
              {"id": "joinByField",
               "options": {"byField": "channel_id", "mode": "outer"}},
              {"id": "convertFieldType",
               "options": {"conversions": [
                   {"targetField": "channel_id", "destinationType": "number"}]}},
              {"id": "sortBy",
               "options": {"sort": [{"field": "channel_id", "desc": False}]}},
              # Value #B is the constant 1 from the modulation info metric, the
              # useful part of which is its label, carried through by the join.
              organize(exclude=["Time"] + [f"Time {i}" for i in range(1, 7)]
                               + ["Value #B"],
                       rename={"channel_id": "Channel", "channel_type": "Type",
                               "modulation": "Modulation",
                               "Value #A": "Power dBmV",
                               "Value #C": "Frequency",
                               "Value #D": "Channel width",
                               "Value #E": "T3 timeouts",
                               "Value #F": "T4 timeouts",
                               "Value #G": "Locked"}),
          ],
          overrides=[
              col("Power dBmV", decimals=1),
              col("Frequency", unit="hertz", color=False),
              col("Channel width", unit="hertz", color=False),
              col("T3 timeouts", tsteps=T3_STEPS, decimals=0),
              col("T4 timeouts", tsteps=T4_STEPS, decimals=0),
              col("Locked", mappings=LOCK_MAP),
          ],
          desc="Every upstream channel. ATDMA reports a centre frequency and "
               "OFDMA a channel width, so each type fills one of those two "
               "columns. T3 and T4 are totals since the modem last booted, not "
               "a rate. Power is not coloured here because the good range "
               "differs by channel type, which the two charts above handle."),

    # --------------------------------------------------------------- event log
    row("Event log and exporter health", 61),
    timeseries("Event log entries by priority", {"h": 8, "w": 8, "x": 0, "y": 62},
               targets(target("modem_event_log_entries", "{{priority}}")),
               step=True, soft=(0, 5),
               desc="Entries currently in the modem's rolling log window. "
                    "These are gauges, not counters, so no rate() on them."),
    timeseries("Parsed log events in window", {"h": 8, "w": 8, "x": 8, "y": 62},
               targets(target("modem_log_events_in_window", "{{type}}")),
               step=True, soft=(0, 5)),
    timeseries("CM-STATUS events in window", {"h": 8, "w": 8, "x": 16, "y": 62},
               targets(target("modem_cm_status_events_in_window",
                              "code {{event_code}}")),
               step=True, soft=(0, 5),
               desc="4 MDD recovery, 20 NCP profile failure, 21 PLC failure, "
                    "22 NCP profile recovery."),
    table("Current channel profiles", {"h": 7, "w": 10, "x": 0, "y": 70},
          targets(target("modem_channel_profile_info", instant=True, table=True)),
          [organize(exclude=["Time", "Value", "__name__", "instance", "job"],
                    rename={"direction": "Direction", "channel_id": "Channel",
                            "profile": "Profile"},
                    order={"direction": 0, "channel_id": 1, "profile": 2})],
          desc="Most recent profile the CMTS assigned per channel, read from "
               "the event log."),
    timeseries("Modem poll duration", {"h": 7, "w": 10, "x": 10, "y": 70},
               targets(target("modem_scrape_duration_seconds", "poll")),
               unit="s", soft=(0, 10),
               desc="How long the exporter needs to read every endpoint. This "
                    "happens on a background thread, so Prometheus scrapes stay "
                    "instant regardless."),
    stat("Last poll age", {"h": 7, "w": 4, "x": 20, "y": 70},
         targets(target("time() - modem_last_poll_timestamp_seconds")),
         unit="s", decimals=0, tsteps=[("green", None), ("red", 120.0)],
         desc="Time since the exporter last reached the modem. Alert on this "
              "going stale."),
]

dashboard = {
    "__inputs": [{
        "name": "DS_PROMETHEUS", "label": "Prometheus", "description": "",
        "type": "datasource", "pluginId": "prometheus",
        "pluginName": "Prometheus"}],
    "__requires": [
        {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.0.0"},
        {"type": "datasource", "id": "prometheus", "name": "Prometheus", "version": "1.0.0"},
        {"type": "panel", "id": "timeseries", "name": "Time series", "version": ""},
        {"type": "panel", "id": "stat", "name": "Stat", "version": ""},
        {"type": "panel", "id": "table", "name": "Table", "version": ""},
    ],
    "annotations": {"list": [{
        "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
        "name": "Annotations & Alerts", "type": "dashboard"}]},
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 1,
    "id": None,
    "links": [],
    "panels": panels,
    "refresh": "1m",
    "schemaVersion": 39,
    "tags": ["docsis", "modem", "f3896lg"],
    "templating": {"list": []},
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "title": "Sagemcom F3896LG",
    "uid": "f3896lg-ziggo",
    "version": 1,
    "weekStart": "",
}

# the generated dashboard belongs at the repo root, one level up from docs/
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "grafana-dashboard.json")
with open(out, "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")
n_panels = sum(1 for p in panels if p["type"] != "row")
print(f"wrote {out}, {n_panels} panels in {len(panels) - n_panels} rows")
