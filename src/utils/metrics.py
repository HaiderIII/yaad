"""Prometheus metrics for observability."""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


@dataclass
class Counter:
    """Simple counter metric."""

    name: str
    help: str
    labels: tuple[str, ...] = ()
    _values: dict[tuple, float] = field(default_factory=lambda: defaultdict(float))

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Increment the counter."""
        label_values = tuple(labels.get(l, "") for l in self.labels)
        self._values[label_values] += amount

    def get(self, **labels: str) -> float:
        """Get counter value."""
        label_values = tuple(labels.get(l, "") for l in self.labels)
        return self._values[label_values]


@dataclass
class Gauge:
    """Simple gauge metric."""

    name: str
    help: str
    labels: tuple[str, ...] = ()
    _values: dict[tuple, float] = field(default_factory=lambda: defaultdict(float))

    def set(self, value: float, **labels: str) -> None:
        """Set the gauge value."""
        label_values = tuple(labels.get(l, "") for l in self.labels)
        self._values[label_values] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Increment the gauge."""
        label_values = tuple(labels.get(l, "") for l in self.labels)
        self._values[label_values] += amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        """Decrement the gauge."""
        label_values = tuple(labels.get(l, "") for l in self.labels)
        self._values[label_values] -= amount

    def get(self, **labels: str) -> float:
        """Get gauge value."""
        label_values = tuple(labels.get(l, "") for l in self.labels)
        return self._values[label_values]


@dataclass
class Histogram:
    """Simple histogram metric with predefined buckets."""

    name: str
    help: str
    labels: tuple[str, ...] = ()
    buckets: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
    _counts: dict[tuple, dict[float, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    _sums: dict[tuple, float] = field(default_factory=lambda: defaultdict(float))
    _totals: dict[tuple, int] = field(default_factory=lambda: defaultdict(int))

    def observe(self, value: float, **labels: str) -> None:
        """Observe a value."""
        label_values = tuple(labels.get(l, "") for l in self.labels)

        # Update sum and count
        self._sums[label_values] += value
        self._totals[label_values] += 1

        # Update buckets
        for bucket in self.buckets:
            if value <= bucket:
                self._counts[label_values][bucket] += 1


class MetricsRegistry:
    """Registry for all metrics."""

    def __init__(self):
        # HTTP metrics
        self.http_requests_total = Counter(
            name="http_requests_total",
            help="Total number of HTTP requests",
            labels=("method", "path", "status"),
        )

        self.http_request_duration_seconds = Histogram(
            name="http_request_duration_seconds",
            help="HTTP request duration in seconds",
            labels=("method", "path"),
        )

        self.http_requests_in_progress = Gauge(
            name="http_requests_in_progress",
            help="Number of HTTP requests in progress",
            labels=("method",),
        )

        # Database metrics
        self.db_queries_total = Counter(
            name="db_queries_total",
            help="Total number of database queries",
            labels=("operation",),
        )

        # External API metrics
        self.external_api_requests_total = Counter(
            name="external_api_requests_total",
            help="Total number of external API requests",
            labels=("service", "status"),
        )

        self.external_api_duration_seconds = Histogram(
            name="external_api_duration_seconds",
            help="External API request duration in seconds",
            labels=("service",),
        )

        # Background task metrics
        self.background_task_runs_total = Counter(
            name="background_task_runs_total",
            help="Total number of background task runs",
            labels=("task", "status"),
        )

        # Media metrics
        self.media_items_total = Gauge(
            name="media_items_total",
            help="Total number of media items",
            labels=("type", "status"),
        )

        self.users_total = Gauge(
            name="users_total",
            help="Total number of users",
        )

    def format_prometheus(self) -> str:
        """Format all metrics in Prometheus exposition format."""
        lines = []

        for name, metric in self.__dict__.items():
            if isinstance(metric, Counter):
                lines.append(f"# HELP {metric.name} {metric.help}")
                lines.append(f"# TYPE {metric.name} counter")
                for label_values, value in metric._values.items():
                    if metric.labels:
                        labels_str = ",".join(
                            f'{l}="{v}"' for l, v in zip(metric.labels, label_values)
                        )
                        lines.append(f"{metric.name}{{{labels_str}}} {value}")
                    else:
                        lines.append(f"{metric.name} {value}")

            elif isinstance(metric, Gauge):
                lines.append(f"# HELP {metric.name} {metric.help}")
                lines.append(f"# TYPE {metric.name} gauge")
                for label_values, value in metric._values.items():
                    if metric.labels:
                        labels_str = ",".join(
                            f'{l}="{v}"' for l, v in zip(metric.labels, label_values)
                        )
                        lines.append(f"{metric.name}{{{labels_str}}} {value}")
                    else:
                        lines.append(f"{metric.name} {value}")

            elif isinstance(metric, Histogram):
                lines.append(f"# HELP {metric.name} {metric.help}")
                lines.append(f"# TYPE {metric.name} histogram")
                for label_values in metric._sums.keys():
                    if metric.labels:
                        labels_str = ",".join(
                            f'{l}="{v}"' for l, v in zip(metric.labels, label_values)
                        )
                        base_labels = f"{{{labels_str}"
                    else:
                        base_labels = "{"

                    # Bucket values
                    cumulative = 0
                    for bucket in metric.buckets:
                        cumulative += metric._counts[label_values].get(bucket, 0)
                        lines.append(
                            f'{metric.name}_bucket{base_labels},le="{bucket}"}} {cumulative}'
                        )
                    lines.append(f'{metric.name}_bucket{base_labels},le="+Inf"}} {metric._totals[label_values]}')
                    lines.append(f"{metric.name}_sum{base_labels}}} {metric._sums[label_values]}")
                    lines.append(f"{metric.name}_count{base_labels}}} {metric._totals[label_values]}")

        return "\n".join(lines)


# Global metrics registry
metrics = MetricsRegistry()


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware to collect HTTP metrics."""

    async def dispatch(self, request: Request, call_next) -> Response:
        method = request.method
        path = self._normalize_path(request.url.path)

        # Track in-progress requests
        metrics.http_requests_in_progress.inc(method=method)

        start_time = time.monotonic()
        try:
            response = await call_next(request)
            status = str(response.status_code)
        except Exception:
            status = "500"
            raise
        finally:
            duration = time.monotonic() - start_time

            # Record metrics
            metrics.http_requests_total.inc(method=method, path=path, status=status)
            metrics.http_request_duration_seconds.observe(duration, method=method, path=path)
            metrics.http_requests_in_progress.dec(method=method)

        return response

    def _normalize_path(self, path: str) -> str:
        """Normalize path for metric labels (replace IDs with placeholders)."""
        parts = path.split("/")
        normalized = []
        for part in parts:
            if part.isdigit():
                normalized.append(":id")
            else:
                normalized.append(part)
        return "/".join(normalized)
