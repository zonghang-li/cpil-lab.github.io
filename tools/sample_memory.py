"""Low-frequency, read-only memory evidence for an owned native server PID."""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time


class MacRusageV4(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64) for name in (
            "user_time system_time pkg_idle_wkups interrupt_wkups pageins wired_size "
            "resident_size phys_footprint proc_start_abstime proc_exit_abstime "
            "child_user_time child_system_time child_pkg_idle_wkups child_interrupt_wkups "
            "child_pageins child_elapsed_abstime diskio_bytesread diskio_byteswritten "
            "cpu_time_qos_default cpu_time_qos_maintenance cpu_time_qos_background "
            "cpu_time_qos_utility cpu_time_qos_legacy cpu_time_qos_user_initiated "
            "cpu_time_qos_user_interactive billed_system_time serviced_system_time "
            "logical_writes lifetime_max_phys_footprint instructions cycles billed_energy "
            "serviced_energy interval_max_phys_footprint runnable_time").split()]


def mac_memory(pid):
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_pid_rusage
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    info = MacRusageV4()
    if function(pid, 4, ctypes.byref(info)) != 0:
        raise OSError(ctypes.get_errno(), "proc_pid_rusage failed")
    return {"rssBytes": info.resident_size, "physicalFootprintBytes": info.phys_footprint,
            "lifetimePeakPhysicalFootprintBytes": info.lifetime_max_phys_footprint}


class MemorySampler:
    def __init__(self, pid, path):
        self.pid, self.path = pid, path
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        with self.path.open("x", encoding="utf-8") as output:
            while not self.done.is_set():
                row = {"pid": self.pid, "monotonicNs": time.perf_counter_ns()}
                try:
                    if sys.platform == "darwin":
                        row.update(mac_memory(self.pid))
                    elif os.name == "nt":
                        inventory = subprocess.check_output([
                            "nvidia-smi", "-i", "0", "--query-gpu=memory.used,memory.free,utilization.gpu",
                            "--format=csv,noheader,nounits"], text=True, timeout=8)
                        used, free, utilization = [int(value.strip()) for value in inventory.strip().split(",")]
                        row.update(gpuUsedMiB=used, gpuFreeMiB=free, gpuUtilizationPercent=utilization,
                                   memoryScope="GPU0 device-global, includes other applications")
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    row["error"] = str(error)
                output.write(json.dumps(row) + "\n")
                output.flush()
                self.done.wait(2)

    def stop(self):
        self.done.set()
        self.thread.join(timeout=10)
