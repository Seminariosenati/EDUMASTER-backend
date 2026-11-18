"""Uso de CPU y memoria del servidor y del proceso, como el Administrador de tareas, publicado por SSE."""

import asyncio
import os
import threading

import psutil

from app.core.events import bus, envelope

_process = psutil.Process(os.getpid())
_process.cpu_percent(None)
psutil.cpu_percent(None)

training_state = {"active_run": None, "lock": threading.Lock()}


def snapshot() -> dict:
    vm = psutil.virtual_memory()
    with _process.oneshot():
        rss = _process.memory_info().rss
        cpu = _process.cpu_percent(None)
        threads = _process.num_threads()
    return {
        "cpu_percent": psutil.cpu_percent(None),
        "cpu_per_core": psutil.cpu_percent(None, percpu=True),
        "cpu_count": psutil.cpu_count(),
        "memory_total": vm.total,
        "memory_used": vm.total - vm.available,
        "memory_percent": vm.percent,
        "process_memory": rss,
        "process_cpu_percent": cpu,
        "process_threads": threads,
        "training_active": training_state["active_run"] is not None,
        "active_run_id": training_state["active_run"],
    }


async def resource_publisher(interval: float) -> None:
    while True:
        if bus.subscribers:
            data = await asyncio.to_thread(snapshot)
            for org in {s.organization_id for s in bus.subscribers}:
                bus.publish(envelope("system.resources", org, {**data, "audience_roles": None}))
        await asyncio.sleep(interval)
