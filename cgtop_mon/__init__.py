#!/usr/bin/env python3

import fnmatch
import logging
import os
import socket
import subprocess
from time import monotonic

INITIAL_RETRY_DELAY_S = 1
MAX_RETRY_DELAY_S = 60
CLIENT_RESET_FAILURES = 3
DEFAULT_MAX_PENDING_POINTS = 1000


def convert(data_type, string):
    try:
        ret = data_type(string)
    except ValueError:
        return None
    return ret


def build_json_body(cg, tasks, cpu_percent, memory, input_per_sec, output_per_sec):
    fields = {
        "cpu": convert(float, cpu_percent),
        "memory": convert(int, memory),
        "tasks": convert(int, tasks),
        "io_read": convert(int, input_per_sec),
        "io_write": convert(int, output_per_sec),
    }
    fields = {name: value for name, value in fields.items() if value is not None}
    if not fields:
        return None

    cg_split = cg.split("/")
    name = cg_split[-1]
    prefix = "/".join(cg_split[:-1])
    if cg == "/":
        name = "/"

    json_body = {
        "measurement": os.getenv("CGTOP_MON_HOSTNAME", socket.gethostname()),
        "tags": {"name": name},
        "fields": fields,
    }
    if prefix:
        json_body["tags"]["prefix"] = prefix
    return json_body


def parse_row(row):
    line = row.rstrip().split()
    if not line or len(line) != 6:
        return None
    return build_json_body(*line)


def matches_any_pattern(name, patterns):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def is_allowed_name(name, blacklist, whitelist):
    return not matches_any_pattern(name, blacklist) and (
        not whitelist or matches_any_pattern(name, whitelist)
    )


def is_truthy(value):
    return value.lower() in ["yes", "on", "true", "1"]


def build_influxdb_client(client_class):
    ssl = is_truthy(os.getenv("CGTOP_MON_INFLUXDB_SSL", "false"))
    verify_ssl = is_truthy(
        os.getenv("CGTOP_MON_INFLUXDB_VERIFY_SSL", "true" if ssl else "false")
    )
    return client_class(
        os.getenv("CGTOP_MON_INFLUXDB_HOST"),
        os.getenv("CGTOP_MON_INFLUXDB_PORT", 8086),
        os.getenv("CGTOP_MON_INFLUXDB_USER"),
        os.getenv("CGTOP_MON_INFLUXDB_PASSWORD"),
        os.getenv("CGTOP_MON_INFLUXDB_DATABASE"),
        ssl=ssl,
        verify_ssl=verify_ssl,
    )


class InfluxWriter:
    def __init__(
        self,
        client_factory,
        send_buffer_size,
        max_pending_points=DEFAULT_MAX_PENDING_POINTS,
        logger=None,
        now=None,
        initial_retry_delay_s=INITIAL_RETRY_DELAY_S,
        max_retry_delay_s=MAX_RETRY_DELAY_S,
        client_reset_failures=CLIENT_RESET_FAILURES,
    ):
        self.client_factory = client_factory
        self.send_buffer_size = max(1, int(send_buffer_size))
        self.max_pending_points = max(
            self.send_buffer_size, int(max_pending_points)
        )
        self.logger = logger or logging.getLogger(__name__)
        self.now = now or monotonic
        self.initial_retry_delay_s = int(initial_retry_delay_s)
        self.max_retry_delay_s = int(max_retry_delay_s)
        self.client_reset_failures = int(client_reset_failures)

        self.client = None
        self.pending_points = []
        self.consecutive_failures = 0
        self.retry_delay_s = self.initial_retry_delay_s
        self.next_retry_at = 0
        self.degraded_since = None
        self.dropped_points = 0
        self.drop_warning_logged = False

    def enqueue(self, point):
        self.pending_points.append(point)
        overflow = len(self.pending_points) - self.max_pending_points
        if overflow <= 0:
            return

        del self.pending_points[:overflow]
        self.dropped_points += overflow
        if not self.drop_warning_logged:
            self.logger.warning(
                "InfluxDB backlog full while retrying; dropped %s point(s)",
                self.dropped_points,
            )
            self.drop_warning_logged = True

    def flush_ready(self):
        while len(self.pending_points) >= self.send_buffer_size:
            if self.now() < self.next_retry_at:
                return

            if not self._write_batch():
                return

    def _write_batch(self):
        batch = self.pending_points[: self.send_buffer_size]
        try:
            self._get_client().write_points(batch)
        except Exception as error:
            self._handle_write_failure(error)
            return False

        del self.pending_points[: self.send_buffer_size]
        self._handle_write_success()
        return True

    def _get_client(self):
        if self.client is None:
            self.client = self.client_factory()
        return self.client

    def _handle_write_failure(self, error):
        now = self.now()
        retry_in_s = self.retry_delay_s
        first_failure = self.consecutive_failures == 0

        self.consecutive_failures += 1
        self.next_retry_at = now + retry_in_s
        self.retry_delay_s = min(
            self.retry_delay_s * 2,
            self.max_retry_delay_s,
        )
        if self.degraded_since is None:
            self.degraded_since = now

        if first_failure:
            self.logger.warning(
                "InfluxDB write failed; entering degraded mode: %s. Retrying in %ss.",
                error,
                retry_in_s,
            )
        elif retry_in_s < self.max_retry_delay_s:
            self.logger.warning(
                "InfluxDB write still failing after %s consecutive error(s): %s. Retrying in %ss.",
                self.consecutive_failures,
                error,
                retry_in_s,
            )

        if self.client_reset_failures > 0 and (
            self.consecutive_failures % self.client_reset_failures == 0
        ):
            self.client = None
            self.logger.warning(
                "Recreating InfluxDB client after %s consecutive write failure(s).",
                self.consecutive_failures,
            )

    def _handle_write_success(self):
        if self.degraded_since is not None:
            self.logger.info(
                "InfluxDB write recovered after %.1fs; %s consecutive failure(s), %s dropped point(s).",
                self.now() - self.degraded_since,
                self.consecutive_failures,
                self.dropped_points,
            )

        self.consecutive_failures = 0
        self.retry_delay_s = self.initial_retry_delay_s
        self.next_retry_at = 0
        self.degraded_since = None
        self.dropped_points = 0
        self.drop_warning_logged = False


def main():
    from influxdb import InfluxDBClient

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    send_buffer_size = int(os.getenv("CGTOP_MON_SEND_BUFSIZE", 10))
    max_pending_points = int(
        os.getenv("CGTOP_MON_MAX_PENDING_POINTS", DEFAULT_MAX_PENDING_POINTS)
    )
    writer = InfluxWriter(
        lambda: build_influxdb_client(InfluxDBClient),
        send_buffer_size,
        max_pending_points=max_pending_points,
    )

    cmd = [
        "systemd-cgtop",
        "--iterations=0",
        "--order=memory",
        "-b",
        "-r",
        "-d",
        os.getenv("CGTOP_MON_DELAY", "5"),
        os.getenv("CGTOP_MON_GROUP", ""),
    ]

    blacklist = [x for x in os.getenv("CGTOP_MON_BLACKLIST", "").split(",") if x]
    whitelist = [x for x in os.getenv("CGTOP_MON_WHITELIST", "").split(",") if x]

    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True
    ) as p:
        for row in p.stdout:
            json_body = parse_row(row)
            if json_body is None:
                continue
            name = json_body["tags"]["name"]

            if not is_allowed_name(name, blacklist, whitelist):
                continue

            writer.enqueue(json_body)
            writer.flush_ready()


if __name__ == "__main__":
    main()
