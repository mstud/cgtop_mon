#!/usr/bin/env python3

import subprocess
import os
import socket
from influxdb import InfluxDBClient

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

            try:
                json_body = {
                    "measurement": os.getenv("CGTOP_MON_HOSTNAME", socket.gethostname()),
                    "tags": {"name": name},
                    "fields": {
                        "cpu": float(cpu_percent),
                        "memory": int(memory),
                        "tasks": int(tasks),
                    },
                }
                if prefix:
                    json_body["tags"]["prefix"] = prefix
            except ValueError:
                continue
            print(json_body)
            to_send.append(json_body)
            if len(to_send) > SEND_BUFFER_SIZE:
                try:
                    client.write_points(to_send)
                except Exception:
                    continue
                to_send.clear()


if __name__ == "__main__":
    main()
