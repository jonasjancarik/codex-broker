from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_broker.http_api import BrokerServices
from test_broker import config_for


class MetricsTests(unittest.TestCase):
    def test_http_latency_metrics_include_counts_and_endpoint_sums(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            try:
                endpoint = "v1/owners/ownerId/threads/threadId/turns"
                services.scheduler.note_http_request(endpoint, 202, 0.25)
                services.scheduler.note_http_request(endpoint, 409, 0.75)

                metrics = services.scheduler.metrics()
                self.assertEqual(metrics["http_requests_total"], 2)
                self.assertEqual(metrics["http_requests_v1_owners_ownerid_threads_threadid_turns_status_202"], 1)
                self.assertEqual(metrics["http_requests_v1_owners_ownerid_threads_threadid_turns_status_409"], 1)
                self.assertEqual(metrics["http_request_duration_seconds_count"], 2)
                self.assertEqual(metrics["http_request_duration_seconds_sum"], 1.0)
                self.assertEqual(metrics["http_request_duration_seconds_count_v1_owners_ownerid_threads_threadid_turns"], 2)
                self.assertEqual(metrics["http_request_duration_seconds_sum_v1_owners_ownerid_threads_threadid_turns"], 1.0)
            finally:
                services.pool.close_all()
                services.state.close()


if __name__ == "__main__":
    unittest.main()
