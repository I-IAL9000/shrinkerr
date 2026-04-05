"""System metrics collection — CPU, RAM, GPU, disk I/O, network bandwidth."""

import asyncio
import json
import os
import time
from typing import Optional

import psutil


async def get_gpu_metrics() -> Optional[dict]:
    """Get NVIDIA GPU metrics via nvidia-smi."""
    try:
        # Base query (works on all NVIDIA GPUs)
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,name",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return None

        line = stdout.decode().strip()
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            return None

        result = {
            "gpu_util": float(parts[0]),
            "memory_used_mb": float(parts[1]),
            "memory_total_mb": float(parts[2]),
            "memory_percent": round(float(parts[1]) / float(parts[2]) * 100, 1) if float(parts[2]) > 0 else 0,
            "temperature_c": float(parts[3]),
            "power_draw_w": float(parts[4]),
            "power_limit_w": float(parts[5]),
            "name": parts[6],
            "encoder_util": None,
            "decoder_util": None,
        }

        # Try encoder/decoder utilization (not supported on all GPUs)
        try:
            proc2 = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=utilization.enc,utilization.dec",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=3)
            if proc2.returncode == 0:
                enc_parts = [p.strip() for p in stdout2.decode().strip().split(",")]
                if len(enc_parts) >= 2:
                    result["encoder_util"] = float(enc_parts[0])
                    result["decoder_util"] = float(enc_parts[1])
        except Exception:
            pass

        return result
    except Exception:
        return None


def get_cpu_metrics() -> dict:
    """Get CPU utilization and load averages.

    Uses /host_proc/loadavg if available for accurate host load.
    """
    load = list(psutil.getloadavg())
    try:
        if os.path.exists("/host_proc/loadavg"):
            with open("/host_proc/loadavg") as f:
                parts = f.read().strip().split()
                load = [float(parts[0]), float(parts[1]), float(parts[2])]
    except Exception:
        pass

    return {
        "cpu_percent": psutil.cpu_percent(interval=0),
        "cpu_count": psutil.cpu_count(),
        "load_avg": load,
        "cpu_freq_mhz": round(psutil.cpu_freq().current) if psutil.cpu_freq() else None,
    }


def get_memory_metrics() -> dict:
    """Get RAM usage."""
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "ram_total_gb": round(mem.total / (1024**3), 1),
        "ram_used_gb": round(mem.used / (1024**3), 1),
        "ram_percent": mem.percent,
        "swap_used_gb": round(swap.used / (1024**3), 1),
        "swap_percent": swap.percent,
    }


_last_disk_io = None
_last_disk_time = 0.0


def get_disk_io_metrics() -> dict:
    """Get disk I/O rates (read/write MB/s)."""
    global _last_disk_io, _last_disk_time
    now = time.time()

    c = psutil.disk_io_counters()
    if c is None:
        return {"read_mbps": 0, "write_mbps": 0}
    current = (c.read_bytes, c.write_bytes)

    if _last_disk_io is None or now - _last_disk_time < 0.5:
        _last_disk_io = current
        _last_disk_time = now
        return {"read_mbps": 0, "write_mbps": 0}

    elapsed = now - _last_disk_time
    read_mbps = round((current[0] - _last_disk_io[0]) / elapsed / (1024**2), 1)
    write_mbps = round((current[1] - _last_disk_io[1]) / elapsed / (1024**2), 1)

    _last_disk_io = current
    _last_disk_time = now

    return {
        "read_mbps": max(0, read_mbps),
        "write_mbps": max(0, write_mbps),
    }


_last_net_io = None
_last_net_time = 0.0


def get_network_metrics() -> dict:
    """Get network throughput (upload/download MB/s).

    Reads from /host_proc/net/dev if available (host metrics via bind mount),
    falls back to psutil (container-only metrics).
    """
    global _last_net_io, _last_net_time
    now = time.time()

    # Try host proc first (if mounted), then container psutil
    rx_bytes = 0
    tx_bytes = 0
    try:
        # Read from host's /proc/net/dev if mounted
        proc_path = "/host_proc/net/dev" if os.path.exists("/host_proc/net/dev") else "/proc/net/dev"
        with open(proc_path) as f:
            for line in f:
                line = line.strip()
                if ":" not in line or line.startswith("Inter") or line.startswith(" face"):
                    continue
                iface, data = line.split(":", 1)
                iface = iface.strip()
                # Skip loopback and docker/veth interfaces
                if iface in ("lo",) or iface.startswith("veth") or iface.startswith("br-") or iface.startswith("docker"):
                    continue
                parts = data.split()
                if len(parts) >= 9:
                    rx_bytes += int(parts[0])
                    tx_bytes += int(parts[8])
    except Exception:
        # Fallback to psutil
        counters = psutil.net_io_counters()
        rx_bytes = counters.bytes_recv
        tx_bytes = counters.bytes_sent

    current = (rx_bytes, tx_bytes)

    if _last_net_io is None or now - _last_net_time < 0.1:
        _last_net_io = current
        _last_net_time = now
        return {"download_mbps": 0, "upload_mbps": 0}

    elapsed = now - _last_net_time
    dl = round((current[0] - _last_net_io[0]) / elapsed / (1024**2), 1)
    ul = round((current[1] - _last_net_io[1]) / elapsed / (1024**2), 1)

    _last_net_io = current
    _last_net_time = now

    return {
        "download_mbps": max(0, dl),
        "upload_mbps": max(0, ul),
    }


async def get_all_metrics() -> dict:
    """Collect all system metrics in one call."""
    gpu = await get_gpu_metrics()
    return {
        "gpu": gpu,
        "cpu": get_cpu_metrics(),
        "memory": get_memory_metrics(),
        "disk_io": get_disk_io_metrics(),
        "network": get_network_metrics(),
        "timestamp": time.time(),
    }
