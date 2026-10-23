"""Webhook notifications (Slack-compatible JSON) for run events. Never blocks or fails a run."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

log = logging.getLogger("ballast.notify")


class Notifier:
    def __init__(self, url: str, events: list[str], timeout: float, product_name: str, public_url: str = ""):
        self.url = url
        self.events = set(events)
        self.timeout = timeout
        self.product_name = product_name
        self.public_url = public_url.rstrip("/")
        self.sent: list[dict] = []

    def wants(self, event: str) -> bool:
        return bool(self.url) and event in self.events

    def link(self, run_id: str) -> str:
        return f"{self.public_url}/#/runs/{run_id}" if self.public_url else ""

    def payload(self, event: str, run: dict, text: str) -> dict:
        link = self.link(run["id"])
        return {
            "text": f"{self.product_name}: {text}" + (f" <{link}|open report>" if link else ""),
            "event": event,
            "run": {k: run.get(k) for k in ("id", "name", "status", "target_name", "stop_reason")} | ({"url": link} if link else {}),
        }

    def notify(self, event: str, run: dict, text: str) -> None:
        if not self.wants(event):
            return
        body = self.payload(event, run, text)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._post(body)
            return
        loop.run_in_executor(None, self._post, body)

    def _post(self, body: dict) -> None:
        data = json.dumps(body).encode()
        request = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"}, method="POST")  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                response.read(1024)
            self.sent.append(body)
        except Exception as exc:
            log.warning("webhook for %s failed: %s", body.get("event"), exc)


def from_config(cfg) -> Notifier | None:
    n = cfg.notifications
    if not n.webhook_url:
        return None
    return Notifier(n.webhook_url, list(n.events), n.timeout_seconds, cfg.branding.product_name, cfg.server.public_url)
