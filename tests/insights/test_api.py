"""Tests for insights API metadata parsing and report access."""

import contextlib
import json
from typing import Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencortex.http.request_context import (
    reset_request_identity,
    set_request_identity,
)
from opencortex.insights.api import create_insights_router


class DummyAgent:
    async def analyze_async(self, *args, **kwargs):
        raise NotImplementedError


class DummyCortexFS:
    def __init__(self, storage: Optional[Dict[str, str]] = None):
        self.storage = storage or {}

    async def read(self, uri: str) -> Optional[str]:
        return self.storage.get(uri)


class DummyReportManager:
    def __init__(
        self,
        latest: Optional[Dict[str, str]] = None,
        history: Optional[List[Dict[str, str]]] = None,
        cortex_storage: Optional[Dict[str, str]] = None,
    ):
        self._latest = latest
        self._history = history or []
        self._cortex_fs = DummyCortexFS(cortex_storage or {})

    async def get_latest_report(self, tenant_id: str, user_id: str):
        return self._latest

    async def get_report_history(self, tenant_id: str, user_id: str, limit: int = 10):
        return self._history

    async def save_report(self, *_args, **_kwargs):
        return {}


@contextlib.contextmanager
def identity_context(tenant_id: str, user_id: str):
    tokens = set_request_identity(tenant_id, user_id)
    try:
        yield
    finally:
        reset_request_identity(tokens)


@contextlib.contextmanager
def insights_client(report_manager: DummyReportManager):
    app = FastAPI()
    router = create_insights_router(
        agent=DummyAgent(), report_manager=report_manager, orchestrator=None
    )
    app.include_router(router)

    with TestClient(app) as client:
        yield client


def test_latest_accepts_to_period_format():
    latest_report = {
        "json_uri": "opencortex://tenant1/user1/insights/reports/2024-05-01/weekly.json",
        "generated_at": "2024-05-01T00:00:00",
        "report_period": "2024-05-01 to 2024-05-07",
        "total_sessions": 1,
        "total_messages": 2,
    }
    report_manager = DummyReportManager(latest=latest_report)

    with insights_client(report_manager) as client:
        with identity_context("tenant1", "user1"):
            response = client.get("/api/v1/insights/latest")

    data = response.json()
    report = data["report"]
    assert report["period_start"] == "2024-05-01"
    assert report["period_end"] == "2024-05-07"


def test_history_accepts_to_period_format():
    history = [
        {
            "json_uri": "opencortex://tenant1/user1/insights/reports/2024-05-01/weekly.json",
            "generated_at": "2024-05-01T00:00:00",
            "report_period": "2024-05-01 to 2024-05-07",
            "total_sessions": 1,
            "total_messages": 2,
        }
    ]
    report_manager = DummyReportManager(history=history)

    with insights_client(report_manager) as client:
        with identity_context("tenant1", "user1"):
            response = client.get("/api/v1/insights/history")

    data = response.json()
    assert len(data["reports"]) == 1
    report = data["reports"][0]
    assert report["period_start"] == "2024-05-01"
    assert report["period_end"] == "2024-05-07"


def test_report_rejects_cross_tenant_uri():
    report_manager = DummyReportManager()

    with insights_client(report_manager) as client:
        with identity_context("tenant1", "user1"):
            response = client.get(
                "/api/v1/insights/report",
                params={
                    "report_uri": "opencortex://tenant2/user2/insights/reports/2024-05-01/weekly.json"
                },
            )

    assert response.status_code == 403


def test_report_returns_owner_content():
    report_uri = "opencortex://tenant1/user1/insights/reports/2024-05-01/weekly.json"
    report_manager = DummyReportManager(
        cortex_storage={report_uri: json.dumps({"foo": "bar"})}
    )

    with insights_client(report_manager) as client:
        with identity_context("tenant1", "user1"):
            response = client.get(
                "/api/v1/insights/report", params={"report_uri": report_uri}
            )

    assert response.status_code == 200
    assert response.json() == {"foo": "bar"}
