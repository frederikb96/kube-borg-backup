"""Shared pod monitoring library for controller and CLI.

Provides background thread-based monitoring of Kubernetes pods with:
- Real-time event streaming
- Real-time log streaming
- Graceful shutdown via threading.Event
"""

import sys
import threading
import time
from kubernetes import client, watch
from kubernetes.client.exceptions import ApiException


def log_msg(msg: str) -> None:
    """Print message to stderr for logging (matches controller behavior)."""
    print(msg, file=sys.stderr, flush=True)


class PodMonitor:
    """Monitor a Kubernetes pod with event and log streaming.

    Usage:
        monitor = PodMonitor(v1_client, pod_name, namespace)
        monitor.start()

        # Main thread does its own wait logic
        while True:
            pod = v1_client.read_namespaced_pod_status(pod_name, namespace)
            if pod.status.phase in {'Succeeded', 'Failed'}:
                break
            time.sleep(5)

        monitor.stop()
    """

    def __init__(self, v1: client.CoreV1Api, pod_name: str, namespace: str):
        """Initialize pod monitor.

        Args:
            v1: Kubernetes CoreV1Api client
            pod_name: Name of pod to monitor
            namespace: Kubernetes namespace
        """
        self.v1 = v1
        self.pod_name = pod_name
        self.namespace = namespace
        self.stop_event = threading.Event()
        self.log_thread: threading.Thread | None = None
        self.event_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start monitoring threads (events + logs)."""
        self.log_thread = threading.Thread(
            target=self._stream_logs,
            name=f"log-stream-{self.pod_name}",
            daemon=True
        )
        self.event_thread = threading.Thread(
            target=self._stream_events,
            name=f"event-stream-{self.pod_name}",
            daemon=True
        )
        self.log_thread.start()
        self.event_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop monitoring threads gracefully.

        Args:
            timeout: Max seconds to wait for threads to finish
        """
        self.stop_event.set()
        if self.log_thread:
            self.log_thread.join(timeout=timeout)
        if self.event_thread:
            self.event_thread.join(timeout=timeout)

    def _stream_logs(self) -> None:
        """Stream pod logs to stdout in real-time (runs in background thread).

        Copied from controller stream_pod_logs() function.
        """
        try:
            # Poll until container is ready OR stop_event is set
            # No hardcoded timeout - main thread sets stop_event when pod completes
            while not self.stop_event.is_set():
                try:
                    pod = self.v1.read_namespaced_pod(self.pod_name, self.namespace)

                    # Check container status (not just pod phase)
                    if pod.status.container_statuses:
                        for container in pod.status.container_statuses:
                            # Container running - ready to stream!
                            if container.state.running and container.state.running.started_at:
                                break

                            # Container terminated (succeeded/failed) - need fallback
                            if container.state.terminated:
                                break
                        else:
                            # No container ready yet, keep polling
                            time.sleep(2)
                            continue

                        # Found container in ready state, break outer loop
                        break
                except ApiException:
                    pass

                time.sleep(2)

            # If stop_event was set before container ready, exit
            if self.stop_event.is_set():
                return

            # Try streaming logs with follow=True
            try:
                log_stream = self.v1.read_namespaced_pod_log(
                    self.pod_name,
                    self.namespace,
                    follow=True,  # Always try follow first
                    _preload_content=False
                )

                # Stream logs line by line
                for line in log_stream:
                    if self.stop_event.is_set():
                        break
                    line_str = line.decode('utf-8').rstrip('\n\r')
                    if line_str:
                        print(f"[{self.pod_name}] {line_str}", flush=True)

            except ApiException as exc:
                # Handle "Bad Request" - likely pod completed before streaming started
                if hasattr(exc, 'status') and exc.status == 400:
                    # Fallback: Read all logs without follow
                    try:
                        logs = self.v1.read_namespaced_pod_log(self.pod_name, self.namespace)
                        if logs:
                            for line in logs.split('\n'):
                                if line.strip():
                                    print(f"[{self.pod_name}] {line}", flush=True)
                    except ApiException:
                        # Even fallback failed - just log warning
                        if not self.stop_event.is_set():
                            log_msg(f"⚠️  Could not retrieve logs for {self.pod_name}")
                else:
                    # Other error - log it
                    if not self.stop_event.is_set():
                        reason = exc.reason if hasattr(exc, 'reason') else exc
                        log_msg(f"⚠️  Log streaming ended for {self.pod_name}: {reason}")

        except Exception as exc:
            log_msg(f"⚠️  Error streaming logs for {self.pod_name}: {exc}")

    def _stream_events(self) -> None:
        """Stream pod events to stdout in real-time (runs in background thread).

        This function continuously watches for events until stop_event is set.
        It automatically reconnects when watch timeouts occur to ensure
        continuous event monitoring throughout pod lifecycle.

        Copied from controller stream_pod_events() function.
        """
        try:
            latest_resource_version = None

            while not self.stop_event.is_set():
                w = watch.Watch()

                try:
                    # Build kwargs with optional resourceVersion
                    kwargs = {
                        'namespace': self.namespace,
                        'field_selector': f"involvedObject.kind=Pod,involvedObject.name={self.pod_name}",
                        'timeout_seconds': 60
                    }

                    # Resume from last seen event (no duplicates!)
                    if latest_resource_version:
                        kwargs['resource_version'] = latest_resource_version

                    for event in w.stream(self.v1.list_namespaced_event, **kwargs):
                        if self.stop_event.is_set():
                            break

                        obj = event['object']
                        print(f"[EVENT] {obj.reason}: {obj.message}", flush=True)

                        # Track list resource_version from watch response for next reconnect
                        # Using event['object'].metadata.resource_version would cause infinite
                        # event replay on 60s timeout reconnects (Bug #1 - v5.0.7-v5.0.8)
                        if 'raw_object' in event and 'metadata' in event['raw_object']:
                            latest_resource_version = event['raw_object']['metadata'].get('resourceVersion')

                except ApiException as exc:
                    # Ignore status 410 (resource version too old) - normal on reconnect
                    if hasattr(exc, 'status') and exc.status == 410:
                        # Reset resourceVersion, will get fresh events
                        latest_resource_version = None
                        time.sleep(1)
                        continue

                    if not self.stop_event.is_set():
                        reason = exc.reason if hasattr(exc, 'reason') else exc
                        log_msg(f"⚠️  Event watch interrupted for {self.pod_name}: {reason}")
                    break

                finally:
                    w.stop()

                # Brief pause before reconnect
                if not self.stop_event.is_set():
                    time.sleep(1)

        except Exception as exc:
            log_msg(f"⚠️  Error streaming events for {self.pod_name}: {exc}")
