# Monitoring and dashboards

Shrinkerr exposes its numbers two ways: Prometheus metrics, and one small
JSON object for dashboard widgets (Homepage, Homarr and anything else that
can read a JSON URL). Both need the API key (Settings → System →
Authentication) once authentication is set up.

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

Jobs cleared from the Queue with "Clear done" still count.

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
