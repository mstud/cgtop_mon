import os
import socket
import unittest

from cgtop_mon import InfluxWriter, build_json_body, is_allowed_name, parse_row


class ParseRowTest(unittest.TestCase):
    def test_parse_row_includes_io_fields(self):
        row = "/system.slice/demo.service 3 12.5 4096 128 256\n"

        point = parse_row(row)

        self.assertEqual(point["measurement"], socket.gethostname())
        self.assertEqual(
            point["tags"], {"name": "demo.service", "prefix": "/system.slice"}
        )
        self.assertEqual(
            point["fields"],
            {
                "cpu": 12.5,
                "memory": 4096,
                "tasks": 3,
                "io_read": 128,
                "io_write": 256,
            },
        )

    def test_parse_row_skips_unparseable_fields(self):
        row = "/system.slice/demo.service 3 12.5 4096 - -\n"

        point = parse_row(row)

        self.assertEqual(
            point["fields"],
            {
                "cpu": 12.5,
                "memory": 4096,
                "tasks": 3,
            },
        )

    def test_parse_row_keeps_io_only_datapoints(self):
        row = "/system.slice/demo.service - - - 512 1024\n"

        point = parse_row(row)

        self.assertEqual(point["fields"], {"io_read": 512, "io_write": 1024})

    def test_parse_row_rejects_rows_without_metrics(self):
        row = "/system.slice/demo.service - - - - -\n"

        self.assertIsNone(parse_row(row))


class BuildJsonBodyTest(unittest.TestCase):
    def test_root_cgroup_has_root_name_without_prefix(self):
        point = build_json_body("/", "1", "0.5", "1024", "2", "4")

        self.assertEqual(point["tags"], {"name": "/"})

    def test_hostname_override_is_used(self):
        original = os.environ.get("CGTOP_MON_HOSTNAME")
        os.environ["CGTOP_MON_HOSTNAME"] = "override-host"
        try:
            point = build_json_body(
                "/system.slice/demo.service", "1", "0.5", "1024", "2", "4"
            )
        finally:
            if original is None:
                del os.environ["CGTOP_MON_HOSTNAME"]
            else:
                os.environ["CGTOP_MON_HOSTNAME"] = original

        self.assertEqual(point["measurement"], "override-host")


class FilterTest(unittest.TestCase):
    def test_blacklist_blocks_name(self):
        self.assertFalse(is_allowed_name("demo.service", ["demo.service"], []))

    def test_blacklist_supports_wildcards(self):
        self.assertFalse(is_allowed_name("demo-api.service", ["demo-*"], []))

    def test_whitelist_allows_only_selected_names(self):
        self.assertTrue(is_allowed_name("demo.service", [], ["demo.service"]))
        self.assertFalse(is_allowed_name("other.service", [], ["demo.service"]))

    def test_whitelist_supports_wildcards(self):
        self.assertTrue(is_allowed_name("demo.service", [], ["*.service"]))
        self.assertFalse(is_allowed_name("demo.scope", [], ["*.service"]))

    def test_blacklist_takes_precedence_over_whitelist(self):
        self.assertFalse(
            is_allowed_name("demo.service", ["demo.*"], ["*.service"])
        )


class FakeClock:
    def __init__(self):
        self.current = 0

    def now(self):
        return self.current

    def advance(self, seconds):
        self.current += seconds


class FakeLogger:
    def __init__(self):
        self.records = []

    def warning(self, message, *args):
        self.records.append(
            ("warning", message % args if args else message)
        )

    def info(self, message, *args):
        self.records.append(("info", message % args if args else message))


class FakeClient:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def write_points(self, points):
        self.calls.append(list(points))
        result = self.results.pop(0) if self.results else True
        if isinstance(result, Exception):
            raise result
        return result


class FakeClientFactory:
    def __init__(self, client_results):
        self.client_results = [list(results) for results in client_results]
        self.clients = []

    def __call__(self):
        results = self.client_results.pop(0) if self.client_results else []
        client = FakeClient(results)
        self.clients.append(client)
        return client


class InfluxWriterTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.logger = FakeLogger()
        self.points = [
            {"measurement": "node", "tags": {"name": "a"}, "fields": {"cpu": 1}},
            {"measurement": "node", "tags": {"name": "b"}, "fields": {"cpu": 2}},
            {"measurement": "node", "tags": {"name": "c"}, "fields": {"cpu": 3}},
            {"measurement": "node", "tags": {"name": "d"}, "fields": {"cpu": 4}},
            {"measurement": "node", "tags": {"name": "e"}, "fields": {"cpu": 5}},
        ]

    def make_writer(self, client_results, **kwargs):
        factory = FakeClientFactory(client_results)
        writer = InfluxWriter(
            factory,
            kwargs.pop("send_buffer_size", 2),
            max_pending_points=kwargs.pop("max_pending_points", 1000),
            logger=self.logger,
            now=self.clock.now,
            initial_retry_delay_s=kwargs.pop("initial_retry_delay_s", 1),
            max_retry_delay_s=kwargs.pop("max_retry_delay_s", 60),
            client_reset_failures=kwargs.pop("client_reset_failures", 3),
        )
        return writer, factory

    def test_flushes_when_queue_reaches_send_buffer_size(self):
        writer, factory = self.make_writer([[True]])

        writer.enqueue(self.points[0])
        writer.flush_ready()
        self.assertEqual(factory.clients, [])

        writer.enqueue(self.points[1])
        writer.flush_ready()

        self.assertEqual(len(factory.clients), 1)
        self.assertEqual(factory.clients[0].calls, [[self.points[0], self.points[1]]])
        self.assertEqual(writer.pending_points, [])

    def test_first_failure_enters_cooldown_and_skips_immediate_retry(self):
        writer, factory = self.make_writer([[RuntimeError("boom"), True]])

        writer.enqueue(self.points[0])
        writer.enqueue(self.points[1])
        writer.flush_ready()

        self.assertEqual(len(factory.clients[0].calls), 1)
        self.assertEqual(writer.consecutive_failures, 1)
        self.assertEqual(writer.next_retry_at, 1)

        writer.enqueue(self.points[2])
        writer.flush_ready()
        self.assertEqual(len(factory.clients[0].calls), 1)

        self.clock.advance(1)
        writer.flush_ready()
        self.assertEqual(len(factory.clients[0].calls), 2)
        self.assertEqual(writer.pending_points, [self.points[2]])
        self.assertEqual(writer.consecutive_failures, 0)

    def test_repeated_failures_back_off_up_to_maximum(self):
        writer, factory = self.make_writer(
            [[RuntimeError("boom")] * 8],
            send_buffer_size=1,
            client_reset_failures=0,
        )

        writer.enqueue(self.points[0])
        writer.flush_ready()
        self.assertEqual(writer.retry_delay_s, 2)

        expected_delays = [4, 8, 16, 32, 60, 60, 60]
        for expected_delay in expected_delays:
            self.clock.current = writer.next_retry_at
            writer.flush_ready()
            self.assertEqual(writer.retry_delay_s, expected_delay)

        self.assertEqual(len(factory.clients[0].calls), 8)

    def test_recreates_client_after_three_consecutive_failures(self):
        writer, factory = self.make_writer(
            [[RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")], [True]],
            send_buffer_size=1,
        )

        writer.enqueue(self.points[0])
        writer.flush_ready()
        self.clock.current = writer.next_retry_at
        writer.flush_ready()
        self.clock.current = writer.next_retry_at
        writer.flush_ready()

        self.assertEqual(len(factory.clients), 1)
        self.assertIsNone(writer.client)

        self.clock.current = writer.next_retry_at
        writer.flush_ready()

        self.assertEqual(len(factory.clients), 2)
        self.assertEqual(factory.clients[1].calls, [[self.points[0]]])
        self.assertEqual(writer.consecutive_failures, 0)

    def test_success_resets_degraded_state_and_logs_recovery(self):
        writer, _factory = self.make_writer(
            [[RuntimeError("boom"), True]],
            send_buffer_size=1,
        )

        writer.enqueue(self.points[0])
        writer.flush_ready()
        self.clock.advance(1)
        writer.flush_ready()

        self.assertEqual(writer.next_retry_at, 0)
        self.assertIsNone(writer.degraded_since)
        self.assertEqual(writer.dropped_points, 0)
        self.assertFalse(writer.drop_warning_logged)
        self.assertIn(
            ("info", "InfluxDB write recovered after 1.0s; 1 consecutive failure(s), 0 dropped point(s)."),
            self.logger.records,
        )

    def test_pending_queue_is_capped_and_drops_oldest_points(self):
        writer, _factory = self.make_writer(
            [[RuntimeError("boom")]],
            send_buffer_size=2,
            max_pending_points=3,
        )

        writer.enqueue(self.points[0])
        writer.enqueue(self.points[1])
        writer.flush_ready()

        writer.enqueue(self.points[2])
        writer.enqueue(self.points[3])
        writer.enqueue(self.points[4])
        writer.flush_ready()

        self.assertEqual(writer.pending_points, self.points[2:5])
        self.assertEqual(writer.dropped_points, 2)
        self.assertIn(
            ("warning", "InfluxDB backlog full while retrying; dropped 1 point(s)"),
            self.logger.records,
        )


if __name__ == "__main__":
    unittest.main()
