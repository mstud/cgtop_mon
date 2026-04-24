# cgtop_mon

`cgtop_mon` collects CPU, memory, task, and I/O usage from `systemd-cgtop` and
writes the resulting metrics to InfluxDB.

The service is intended to run continuously under systemd. It reads cgroup
statistics in batch mode, filters entries by cgroup name if configured, and
sends points to InfluxDB in small batches.

## Requirements

- Python 3.5 or newer
- `systemd-cgtop`
- Access to an InfluxDB instance
- systemd resource accounting enabled for the units you want to monitor

For useful data, enable the relevant accounting options in systemd, for example
in `/etc/systemd/system.conf`:

```ini
DefaultMemoryAccounting=yes
DefaultCPUAccounting=yes
DefaultIOAccounting=yes
```

After changing accounting defaults, a reboot is usually the simplest way to
apply them broadly. If you enable accounting on an already running system,
existing services may need to be restarted before their memory values become
reliable.

## Installation

Install the package:

```bash
pip install .
```

Install the configuration and service unit:

```bash
cp cgtop_mon.conf /etc/cgtop_mon.conf
cp cgtop_mon.service /etc/systemd/system/cgtop_mon.service
```

Edit `/etc/cgtop_mon.conf` and set at least the required InfluxDB connection
variables.

Enable and start the service:

```bash
systemctl daemon-reload
systemctl enable --now cgtop_mon.service
```

## How It Works

`cgtop_mon` starts `systemd-cgtop` in batch mode and converts each reported
cgroup row into an InfluxDB point.

- The measurement name defaults to the local hostname.
- The `name` tag contains the final cgroup name, such as `sshd.service`.
- The optional `prefix` tag contains the rest of the cgroup path, such as
  `/system.slice`.
- Depending on what `systemd-cgtop` reports, fields can include `cpu`,
  `memory`, `tasks`, `io_read`, and `io_write`.

Rows that contain no usable metrics are skipped. InfluxDB write failures switch
the sender into a degraded mode with exponential backoff, bounded buffering,
and client recreation after repeated failures. Recovery is logged once the
connection works again.

## Configuration

The service unit reads its environment from `/etc/cgtop_mon.conf`.

### Required Variables

| Variable | Description |
| --- | --- |
| `CGTOP_MON_INFLUXDB_HOST` | Hostname or IP address of the InfluxDB server. |
| `CGTOP_MON_INFLUXDB_USER` | InfluxDB username. |
| `CGTOP_MON_INFLUXDB_PASSWORD` | InfluxDB password. |
| `CGTOP_MON_INFLUXDB_DATABASE` | Target InfluxDB database name. |

### Optional Variables

| Variable | Default | Description |
| --- | --- | --- |
| `CGTOP_MON_INFLUXDB_PORT` | `8086` | InfluxDB TCP port. |
| `CGTOP_MON_INFLUXDB_SSL` | `false` | Enable SSL/TLS for the InfluxDB connection. Accepted truthy values are `yes`, `on`, `true`, and `1`. |
| `CGTOP_MON_INFLUXDB_VERIFY_SSL` | `false` when SSL is disabled, otherwise `true` | Enable TLS certificate verification. Uses the same truthy values as `CGTOP_MON_INFLUXDB_SSL`. |
| `CGTOP_MON_DELAY` | `5` | Sampling interval passed to `systemd-cgtop`. |
| `CGTOP_MON_GROUP` | empty | Restrict monitoring to a base cgroup and its subgroups. Example: `system.slice`. |
| `CGTOP_MON_BLACKLIST` | empty | Comma-separated list of cgroup name patterns to exclude. |
| `CGTOP_MON_WHITELIST` | empty | Comma-separated list of cgroup name patterns to include. When set, only matching names are accepted. |
| `CGTOP_MON_SEND_BUFSIZE` | `10` | Number of points buffered before writing to InfluxDB. |
| `CGTOP_MON_MAX_PENDING_POINTS` | `1000` | Maximum number of unsent points kept in memory during an outage. Oldest points are dropped once the limit is reached. |
| `CGTOP_MON_HOSTNAME` | local hostname | Override the measurement name written to InfluxDB. |

### Filter Semantics

`CGTOP_MON_BLACKLIST` and `CGTOP_MON_WHITELIST` match against the final cgroup
name only, not the full path.

Examples:

- `demo.service`
- `*.service`
- `db-*`

Behavior:

- Patterns are comma-separated.
- `*` is supported as a wildcard.
- If `CGTOP_MON_WHITELIST` is empty, all names are allowed unless blacklisted.
- If `CGTOP_MON_WHITELIST` is set, a name must match the whitelist.
- Blacklist matches always win over whitelist matches.

## Example Configuration

```ini
CGTOP_MON_INFLUXDB_HOST=influxdb.example.org
CGTOP_MON_INFLUXDB_PORT=8086
CGTOP_MON_INFLUXDB_USER=cgtop_mon
CGTOP_MON_INFLUXDB_PASSWORD=secret
CGTOP_MON_INFLUXDB_DATABASE=systemd

CGTOP_MON_INFLUXDB_SSL=true
CGTOP_MON_INFLUXDB_VERIFY_SSL=true

CGTOP_MON_DELAY=5
CGTOP_MON_GROUP=system.slice
CGTOP_MON_BLACKLIST=*.scope
CGTOP_MON_WHITELIST=*.service
CGTOP_MON_SEND_BUFSIZE=10
CGTOP_MON_MAX_PENDING_POINTS=1000
CGTOP_MON_HOSTNAME=node-01
```

## Failure Handling

When InfluxDB writes fail, `cgtop_mon`:

- logs a warning when it enters degraded mode
- retries with exponential backoff from 1 second up to 60 seconds
- recreates the InfluxDB client after 3 consecutive write failures
- buffers unsent points in memory up to `CGTOP_MON_MAX_PENDING_POINTS`
- drops the oldest points if the backlog limit is exceeded
- logs a single recovery message with outage duration and drop count after a successful write

## Service Management

Check status:

```bash
systemctl status cgtop_mon.service
```

Restart after configuration changes:

```bash
systemctl restart cgtop_mon.service
```

View logs:

```bash
journalctl -u cgtop_mon.service
```
