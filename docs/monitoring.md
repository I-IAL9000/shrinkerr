# Monitoring and dashboards

Shrinkerr exposes its numbers three ways: Prometheus metrics, one small
JSON object for dashboard widgets (Homepage, Homarr and anything else that
can read a JSON URL), and Home Assistant over MQTT. The first two need the
API key (Settings → System → Authentication) wherever Shrinkerr asks for
sign-in: not with **External (reverse proxy)** sign-in, and not from local
addresses when **Who signs in** is **Not from local addresses**. Send it
in the `X-Api-Key` header (or as below): since 0.10 a key in the URL
(`?api_key=`) isn't accepted.

## Prometheus

`GET /api/metrics` — the Prometheus text format.

```yaml
scrape_configs:
  - job_name: shrinkerr
    metrics_path: /api/metrics
    authorization:              # sent as "Authorization: Bearer <key>"
      credentials: <your Shrinkerr API key>
    static_configs:
      - targets: ["shrinkerr:6680"]
```

`/api/metrics` also takes the key as the Basic-auth password
(`basic_auth:` with any username), for tools that only offer that.

| Metric | Type | What |
|---|---|---|
| `shrinkerr_info{version}` | gauge | The running version (always 1). |
| `shrinkerr_jobs{status}` | gauge | Jobs by status: pending, running, completed (lifetime), failed, cancelled. |
| `shrinkerr_queue_paused` | gauge | 1 while the queue is paused. |
| `shrinkerr_saved_bytes_total` | counter | Space saved by completed jobs, lifetime. |
| `shrinkerr_original_bytes_total` | counter | Original size of the files those jobs converted. |
| `shrinkerr_vmaf_average` | gauge | Average VMAF of completed conversions (absent until there is one). |
| `shrinkerr_encode_fps` | gauge | Average speed of the running jobs; 0 when idle. |
| `shrinkerr_library_files` / `_library_bytes` | gauge | Files in the Scanner, and their size. |
| `shrinkerr_files_to_convert` | gauge | Files that need converting. |
| `shrinkerr_estimated_savings_bytes` | gauge | What converting them would save, estimated. |
| `shrinkerr_node_up{node}` | gauge | Each remote worker: 1 online, 0 offline. |

Completed jobs cleared from the Queue with "Clear done" still count (it
deletes failed and cancelled ones).

## Dashboard widgets

`GET /api/stats/widget` — one flat object:

```json
{
  "version": "0.10.0", "pending": 12, "running": 1, "completed": 4210, "failed": 3,
  "paused": false, "saved_bytes": 9123456789012, "saved_percent": 48.2,
  "library_files": 18342, "to_convert": 911, "estimated_savings_bytes": 2345678901234,
  "fps": 187.5, "vmaf_average": 94.1, "nodes_online": 2, "nodes": 2
}
```

**Homepage** (`services.yaml`, the `customapi` widget):

```yaml
- Shrinkerr:
    href: http://shrinkerr:6680
    widget:
      type: customapi
      url: http://shrinkerr:6680/api/stats/widget
      headers:
        X-Api-Key: <your Shrinkerr API key>
      mappings:
        - field: pending
          label: Queue
        - field: running
          label: Running
        - field: saved_bytes
          label: Saved
          format: bytes
        - field: to_convert
          label: To convert
```

**Homarr** and other dashboards: point a JSON / custom-API widget at the
same URL with an `X-Api-Key` header.

## Home Assistant (MQTT)

Shrinkerr can show up in Home Assistant through MQTT discovery. You need an
MQTT broker that Home Assistant already uses (the Mosquitto add-on, say).

1. Settings → Integrations → **Home Assistant (MQTT)**: tick **Connect to
   the MQTT broker** and enter its host, port (1883, or 8883 with TLS) and
   the user name and password Home Assistant's MQTT integration uses.
2. Click **Test connection** (it saves your changes first).

A **Shrinkerr** device appears in Home Assistant (Settings → Devices &
services → MQTT) with:

| Entity | What it shows |
|---|---|
| Queued jobs, Running jobs, Failed jobs | the queue |
| Completed jobs | conversions finished, all time |
| Space saved | GB saved, all time |
| Files to convert, Estimated savings | what the Scanner found left to do |
| Current file, Progress | the running job (Idle when nothing runs) |
| Queue (switch) | on: the queue runs; off: it's paused |

The numbers update every 15 seconds. Entity names follow
**Notification language** (Settings → Notifications); you can rename them
in Home Assistant.

**TLS and topics:** turn on TLS for a broker that requires it (the
broker's certificate must be one your system trusts). The discovery prefix
must match Home Assistant's (`homeassistant` unless you changed it). Give
each Shrinkerr its own base topic if you run more than one; each becomes
its own device.

Topics, with the default base topic:

- `shrinkerr/status`: `online` / `offline` (offline also when Shrinkerr
  stops or loses the connection)
- `shrinkerr/state`: everything above as JSON
- `shrinkerr/queue/set`: send `ON` or `OFF` to start or pause the queue
