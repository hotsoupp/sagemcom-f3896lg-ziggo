# Metrics reference

All metrics exposed by `exporter.py` on `/metrics`.

| Metric                                   | Description |
|------------------------------------------|-------------|
| `modem_info`                             | Model, firmware and DOCSIS version as labels |
| `modem_uptime_seconds`                   | Modem uptime |
| `modem_boot_time_seconds`                | Boot time; stable across scrapes, good for alerting |
| `modem_bridge_mode`                      | 1 if the modem is in bridge mode |
| `modem_provisioned_rate_bps`             | Provisioned down/up rate the CMTS gives you |
| `modem_provisioned_max_burst_bytes`      | Provisioned burst size |
| `modem_downstream_power_dbmv`            | Downstream power per channel |
| `modem_downstream_snr_db`                | Downstream SNR (SC-QAM) |
| `modem_downstream_rxmer_db`              | Downstream RxMER |
| `modem_downstream_frequency_hz`          | Downstream centre frequency (SC-QAM) |
| `modem_downstream_channel_width_hz`      | Downstream channel width (OFDM) |
| `modem_downstream_locked`                | Downstream lock status |
| `modem_downstream_corrected_errors_total`| FEC-corrected codewords since boot |
| `modem_downstream_uncorrected_errors_total`| Uncorrectable codewords since boot |
| `modem_upstream_power_dbmv`              | Upstream power per channel |
| `modem_upstream_frequency_hz`            | Upstream centre frequency (ATDMA) |
| `modem_upstream_channel_width_hz`        | Upstream channel width (OFDMA) |
| `modem_upstream_locked`                  | Upstream lock status |
| `modem_upstream_symbol_rate_ksps`        | Upstream symbol rate (ATDMA) |
| `modem_upstream_ranging_timeouts_total`  | T1-T4 ranging timeouts since boot |
| `modem_channel_modulation_info`          | Current modulation per channel |
| `modem_channel_profile_info`             | Most recent OFDM/OFDMA profile per channel |
| `modem_event_log_entries`                | Event log entries by priority |
| `modem_log_events_in_window`             | Parsed event types in the log window |
| `modem_cm_status_events_in_window`       | CM-STATUS events by DOCSIS type code |
| `modem_up`                               | 1 if the last modem poll succeeded |
| `modem_endpoint_up`                      | 1 if an endpoint answered in the last poll |
| `modem_scrape_duration_seconds`          | How long the last poll took |
| `modem_last_poll_timestamp_seconds`      | When the last poll happened |
| `modem_last_success_timestamp_seconds`   | When required modem endpoints last succeeded |

A note on the event-log metrics: the modem's event log is a rolling window, so
older entries drop off over time. Those metrics are gauges counting what is
currently in the window, not lifetime counters, so do not wrap them in `rate()`.
