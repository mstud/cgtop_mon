#!/usr/bin/env python3

import subprocess
import os
import socket
from time import time
from influxdb import InfluxDBClient

LOG_THROTTLE_RATE_S = 60

def convert(data_type, string):
    try:
        ret = data_type(string)
    except ValueError:
        return None
    return ret

def main():
    send_buffer_size = os.getenv("CGTOP_MON_SEND_BUFSIZE", 10)

    client = InfluxDBClient(
        os.getenv("CGTOP_MON_INFLUXDB_HOST"),
        os.getenv("CGTOP_MON_INFLUXDB_PORT", 8086),
        os.getenv("CGTOP_MON_INFLUXDB_USER"),
        os.getenv("CGTOP_MON_INFLUXDB_PASSWORD"),
        os.getenv("CGTOP_MON_INFLUXDB_DATABASE"),
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
            line = row.rstrip().split()
            if not line:
                continue
            cg, tasks, cpu_percent, memory, input_per_sec, output_per_sec = line

            cg_split = cg.split("/")
            name = cg_split[-1]
            prefix = "/".join(cg_split[:-1])
            if cg == "/":
                name = "/"

            if name in blacklist or (whitelist and (name not in whitelist)):
                continue

            json_body = {
                "measurement": os.getenv("CGTOP_MON_HOSTNAME", socket.gethostname()),
                "tags": {"name": name},
                "fields": {
                    "cpu": convert(float, cpu_percent),
                    "memory": convert(int, memory),
                    "tasks": convert(int, tasks),
                },
            }
            if prefix:
                json_body["tags"]["prefix"] = prefix

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
