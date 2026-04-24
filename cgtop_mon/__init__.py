#!/usr/bin/env python3

import fnmatch
import subprocess
import os
import socket
from time import time

LOG_THROTTLE_RATE_S = 60


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


def main():
    from influxdb import InfluxDBClient

    send_buffer_size = int(os.getenv("CGTOP_MON_SEND_BUFSIZE", 10))

    ssl = (
        True
        if os.getenv("CGTOP_MON_INFLUXDB_SSL", "false").lower()
        in ["yes", "on", "true", "1"]
        else False
    )
    verify_ssl = (
        True
        if os.getenv(
            "CGTOP_MON_INFLUXDB_VERIFY_SSL", "true" if ssl else "false"
        ).lower()
        in ["yes", "on", "true", "1"]
        else False
    )
    client = InfluxDBClient(
        os.getenv("CGTOP_MON_INFLUXDB_HOST"),
        os.getenv("CGTOP_MON_INFLUXDB_PORT", 8086),
        os.getenv("CGTOP_MON_INFLUXDB_USER"),
        os.getenv("CGTOP_MON_INFLUXDB_PASSWORD"),
        os.getenv("CGTOP_MON_INFLUXDB_DATABASE"),
        ssl=ssl,
        verify_ssl=verify_ssl,
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

    last_log_time = 0
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True
    ) as p:
        to_send = []
        for row in p.stdout:
            json_body = parse_row(row)
            if json_body is None:
                continue
            name = json_body["tags"]["name"]

            if not is_allowed_name(name, blacklist, whitelist):
                continue

            to_send.append(json_body)
            if len(to_send) > send_buffer_size:
                try:
                    client.write_points(to_send)
                except Exception as e:
                    now = time()
                    if now - last_log_time > LOG_THROTTLE_RATE_S:
                        print(e)
                        last_log_time = now
                    continue
                to_send.clear()


if __name__ == "__main__":
    main()
