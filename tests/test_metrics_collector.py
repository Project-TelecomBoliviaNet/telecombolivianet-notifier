"""
Tests para MetricsCollector y las funciones públicas record_sent/record_failed.
"""
from app.services.health import MetricsCollector, record_sent, record_failed, _metrics


class TestMetricsCollector:
    def test_initial_values_are_zero(self):
        collector = MetricsCollector()
        assert collector.sent == 0
        assert collector.failed == 0

    def test_record_sent_increments(self):
        collector = MetricsCollector()
        collector.record_sent()
        assert collector.sent == 1

    def test_record_failed_increments(self):
        collector = MetricsCollector()
        collector.record_failed()
        assert collector.failed == 1

    def test_multiple_records(self):
        collector = MetricsCollector()
        for _ in range(5):
            collector.record_sent()
        for _ in range(3):
            collector.record_failed()
        assert collector.sent == 5
        assert collector.failed == 3

    def test_sent_and_failed_are_independent(self):
        collector = MetricsCollector()
        collector.record_sent()
        collector.record_sent()
        collector.record_failed()
        assert collector.sent == 2
        assert collector.failed == 1

    def test_instances_are_independent(self):
        c1 = MetricsCollector()
        c2 = MetricsCollector()
        c1.record_sent()
        assert c2.sent == 0


class TestPublicCounterFunctions:
    def test_record_sent_updates_global_metrics(self):
        before = _metrics.sent
        record_sent()
        assert _metrics.sent == before + 1

    def test_record_failed_updates_global_metrics(self):
        before = _metrics.failed
        record_failed()
        assert _metrics.failed == before + 1
