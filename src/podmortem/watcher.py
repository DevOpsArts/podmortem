"""Kubernetes pod restart watcher - core logic."""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from kubernetes import client, config, watch

from podmortem.storage import RestartRecord, init_db, store_restart

logger = logging.getLogger("podmortem")


class PodRestartWatcher:
    """Watches Kubernetes pods for restarts and captures diagnostics."""

    def __init__(self, namespace: Optional[str] = None, db_path=None):
        self.namespace = namespace or ""
        self.conn = init_db(db_path)
        self._restart_counts: dict[str, int] = {}

        try:
            config.load_incluster_config()
            self._fix_bearer_token_auth()
            logger.info("Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            config.load_kube_config()
            self._fix_bearer_token_auth()
            logger.info("Loaded local kubeconfig")

        self.v1 = client.CoreV1Api()

    @staticmethod
    def _fix_bearer_token_auth() -> None:
        """Fix kubernetes client v36+ auth_settings mismatch.

        load_kube_config() stores token as api_key['authorization'] but
        auth_settings() expects api_key['BearerToken']. Patch it.
        """
        cfg = client.Configuration.get_default_copy()
        if "authorization" in cfg.api_key and "BearerToken" not in cfg.api_key:
            token = cfg.api_key["authorization"]
            # Strip 'Bearer ' or 'bearer ' prefix if present; auth_settings re-adds it
            if token.lower().startswith("bearer "):
                token = token[len("bearer "):]
            cfg.api_key["BearerToken"] = token
            cfg.api_key_prefix["BearerToken"] = "Bearer"
            client.Configuration.set_default(cfg)

    def _pod_key(self, pod: client.V1Pod, container_name: str) -> str:
        return f"{pod.metadata.namespace}/{pod.metadata.name}/{container_name}"

    def _get_pod_logs(
        self, name: str, namespace: str, container: str, tail_lines: int = 50
    ) -> str:
        """Fetch previous container logs (logs from the crashed container)."""
        try:
            return self.v1.read_namespaced_pod_log(
                name=name,
                namespace=namespace,
                container=container,
                previous=True,
                tail_lines=tail_lines,
            )
        except client.ApiException as e:
            logger.warning("Failed to fetch logs for %s/%s: %s", namespace, name, e.reason)
            return f"<unavailable: {e.reason}>"

    def _get_pod_events(self, name: str, namespace: str) -> str:
        """Fetch recent events related to this pod."""
        try:
            field_selector = f"involvedObject.name={name},involvedObject.namespace={namespace}"
            events = self.v1.list_namespaced_event(
                namespace=namespace,
                field_selector=field_selector,
            )
            lines = []
            for event in events.items:
                lines.append(
                    f"[{event.last_timestamp or event.event_time}] "
                    f"{event.reason}: {event.message}"
                )
            return "\n".join(lines[-20:]) if lines else "<no events>"
        except client.ApiException as e:
            logger.warning("Failed to fetch events for %s/%s: %s", namespace, name, e.reason)
            return f"<unavailable: {e.reason}>"

    def _process_pod(self, pod: client.V1Pod) -> None:
        """Check a pod for new restarts and record them."""
        if not pod.status or not pod.status.container_statuses:
            return

        for cs in pod.status.container_statuses:
            key = self._pod_key(pod, cs.name)
            current_count = cs.restart_count or 0

            prev_count = self._restart_counts.get(key, 0)
            self._restart_counts[key] = current_count

            if current_count > prev_count and prev_count > 0:
                self._record_restart(pod, cs)
            elif key not in self._restart_counts or (
                current_count > 0 and prev_count == 0
            ):
                # First time seeing this pod with restarts already > 0
                self._restart_counts[key] = current_count

    def _record_restart(self, pod: client.V1Pod, cs) -> None:
        """Capture and store a restart event."""
        reason = "Unknown"
        exit_code = None

        if cs.last_state and cs.last_state.terminated:
            terminated = cs.last_state.terminated
            reason = terminated.reason or "Unknown"
            exit_code = terminated.exit_code

        logs = self._get_pod_logs(
            pod.metadata.name, pod.metadata.namespace, cs.name
        )
        events = self._get_pod_events(pod.metadata.name, pod.metadata.namespace)

        record = RestartRecord(
            pod_name=pod.metadata.name,
            namespace=pod.metadata.namespace,
            container_name=cs.name,
            restart_count=cs.restart_count,
            reason=reason,
            exit_code=exit_code,
            last_logs=logs,
            events=events,
            node_name=pod.spec.node_name if pod.spec else None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        row_id = store_restart(self.conn, record)
        logger.info(
            "Recorded restart #%d for %s/%s (container=%s, reason=%s) [row=%d]",
            cs.restart_count,
            pod.metadata.namespace,
            pod.metadata.name,
            cs.name,
            reason,
            row_id,
        )

    def _seed_restart_counts(self) -> None:
        """Seed initial restart counts so we only capture new restarts."""
        logger.info("Seeding initial restart counts...")
        if self.namespace:
            pods = self.v1.list_namespaced_pod(self.namespace)
        else:
            pods = self.v1.list_pod_for_all_namespaces()

        for pod in pods.items:
            if not pod.status or not pod.status.container_statuses:
                continue
            for cs in pod.status.container_statuses:
                key = self._pod_key(pod, cs.name)
                self._restart_counts[key] = cs.restart_count or 0

        logger.info("Seeded %d container restart counts", len(self._restart_counts))

    def run(self) -> None:
        """Start watching for pod restarts (blocking)."""
        self._seed_restart_counts()
        logger.info(
            "Watching for pod restarts (namespace=%s)...",
            self.namespace or "all",
        )

        w = watch.Watch()
        while True:
            try:
                if self.namespace:
                    stream = w.stream(
                        self.v1.list_namespaced_pod,
                        self.namespace,
                        timeout_seconds=300,
                    )
                else:
                    stream = w.stream(
                        self.v1.list_pod_for_all_namespaces,
                        timeout_seconds=300,
                    )

                for event in stream:
                    if event["type"] in ("ADDED", "MODIFIED"):
                        self._process_pod(event["object"])

            except client.ApiException as e:
                logger.error("API error: %s. Retrying in 5s...", e.reason)
                time.sleep(5)
            except Exception as e:
                logger.exception("Unexpected error: %s. Retrying in 5s...", e)
                time.sleep(5)
