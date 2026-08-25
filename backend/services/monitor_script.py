#!/usr/bin/env python3
"""
System Resource Monitor - Logs system stats to help diagnose SSH connectivity issues.

This script monitors:
- CPU usage
- Memory usage
- Disk usage
- Network connectivity
- SSH service status
- System load
- Active connections

Run continuously to capture when the system becomes unreachable.
"""

import logging
import time
from pathlib import Path

import psutil

# Setup logging to file with rotation
log_file = Path("/var/log/system_monitor.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()  # Also print to console
    ]
)

def check_ssh_service() -> dict:
    """
    Check if SSH service is running.

    Returns:
        dict: Service status information
    """
    try:
        import subprocess
        result = subprocess.run(
            ['systemctl', 'is-active', 'ssh'],
            capture_output=True,
            text=True,
            timeout=5
        )
        return {
            'running': result.returncode == 0,
            'status': result.stdout.strip()
        }
    except Exception as e:
        return {'running': False, 'error': str(e)}

def get_system_stats() -> dict:
    """
    Collect current system statistics.

    Returns:
        dict: System statistics including CPU, memory, disk, network
    """
    # CPU usage
    cpu_percent = psutil.cpu_percent(interval=1)
    cpu_count = psutil.cpu_count()

    # Memory usage
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Disk usage
    disk = psutil.disk_usage('/')

    # Network stats
    net_io = psutil.net_io_counters()

    # System load (1, 5, 15 minute averages)
    load_avg = psutil.getloadavg()

    # Number of connections
    connections = len(psutil.net_connections())

    return {
        'cpu_percent': cpu_percent,
        'cpu_count': cpu_count,
        'memory_percent': memory.percent,
        'memory_available_gb': memory.available / (1024**3),
        'swap_percent': swap.percent,
        'disk_percent': disk.percent,
        'disk_free_gb': disk.free / (1024**3),
        'network_bytes_sent': net_io.bytes_sent,
        'network_bytes_recv': net_io.bytes_recv,
        'load_1min': load_avg[0],
        'load_5min': load_avg[1],
        'load_15min': load_avg[2],
        'connections': connections
    }

def monitor_loop(interval_seconds: int = 60):
    """
    Main monitoring loop that logs system stats at regular intervals.

    Args:
        interval_seconds: How often to log stats (default: 60 seconds)
    """
    logging.info("Starting system monitoring...")

    while True:
        try:
            stats = get_system_stats()
            ssh_status = check_ssh_service()

            # Log current stats
            log_message = (
                f"CPU: {stats['cpu_percent']:.1f}% | "
                f"MEM: {stats['memory_percent']:.1f}% ({stats['memory_available_gb']:.2f}GB free) | "
                f"DISK: {stats['disk_percent']:.1f}% ({stats['disk_free_gb']:.2f}GB free) | "
                f"LOAD: {stats['load_1min']:.2f} {stats['load_5min']:.2f} {stats['load_15min']:.2f} | "
                f"CONN: {stats['connections']} | "
                f"SSH: {ssh_status.get('status', 'unknown')}"
            )

            # Warning thresholds
            if stats['cpu_percent'] > 90:
                logging.warning(f"HIGH CPU! {log_message}")
            elif stats['memory_percent'] > 90:
                logging.warning(f"HIGH MEMORY! {log_message}")
            elif stats['disk_percent'] > 90:
                logging.warning(f"HIGH DISK USAGE! {log_message}")
            elif stats['load_1min'] > stats['cpu_count'] * 2:
                logging.warning(f"HIGH LOAD! {log_message}")
            elif not ssh_status.get('running'):
                logging.error(f"SSH SERVICE DOWN! {log_message}")
            else:
                logging.info(log_message)

            time.sleep(interval_seconds)

        except Exception as e:
            logging.error(f"Error in monitoring loop: {e}")
            time.sleep(interval_seconds)

if __name__ == "__main__":
    monitor_loop(interval_seconds=60)  # Log every 60 seconds
