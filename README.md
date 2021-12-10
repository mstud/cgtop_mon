# cgtop_mon

Get CPU and memory usage from systemd-cgtop and write to an InfluxDB database.

MemoryAccounting and CPUAccounting in systemd must be enabled for this to work.
e.g., in `/etc/systemd/system.conf` set:
```
DefaultMemoryAccounting=yes
DefaultCPUAccounting=yes
```
and probably reboot (or activate it on a per-unit level via `systemctl set-property ...`). Note that memory values for already running services might be bogus until restarted when MemoryAccounting is activated mid-runtime.

## Installation

- `pip install .`
- edit `cgtop_mon.conf` and copy it to `/etc`
- copy `cgtop_mon.service` to `/etc/systemd/system`
- `systemctl enable --now cgtop_mon.service`
