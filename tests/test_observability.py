#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import create_app
from app.observability import build_overview
from app.states import State
from app.store import Store


def make_run(store, env="sim", number=1):
    with store.transaction() as conn:
        task = store.upsert_task(
            conn,
            repo="owner/repo",
            issue_number=number,
            title='<script>alert("x")</script>',
            issue_state="open",
            labels=[],
        )
        run = store.create_run(conn, task_id=task, state=State.QUEUED, env=env)
    return run


def test_isolation_unknown_usage_and_empty_period(store):
    make_run(store)
    make_run(store, "live", 2)
    report = build_overview(store, env="sim")
    assert report["run_count"] == 1
    assert report["known_acus"] is None
    assert report["usage_unknown_runs"] == 1
    assert len(report["daily_throughput"]) == 7
    assert all(day["merged"] == 0 for day in report["daily_throughput"])
    assert build_overview(store, repo="absent/repo")["run_count"] == 0


def test_events_atomic_noop_and_throughput(store):
    run = make_run(store)
    with store.transaction() as conn:
        store.update_run(conn, run, state="pr_open", pr_number=10)
        store.update_run(conn, run, state="pr_open")
        store.update_run(conn, run, state="merged", merged_sha="abc")
    assert len(store.events_for_run(run)) == 3
    report = build_overview(store)
    assert sum(day["pr_opened"] for day in report["daily_throughput"]) == 1
    assert sum(day["merged"] for day in report["daily_throughput"]) == 1
    assert report["runs"][0]["checks"] == "not evaluated"
    try:
        with store.transaction() as conn:
            store.update_run(conn, run, state="failed")
            raise ValueError("rollback")
    except ValueError:
        pass
    assert len(store.events_for_run(run)) == 3


def test_restart_and_legacy_snapshot(tmp_path):
    path = str(tmp_path / "pipeline.db")
    store = Store(path)
    run = make_run(store)
    with store.transaction() as conn:
        conn.execute("DELETE FROM run_events")
        store.update_run(conn, run, pr_number=1)
    store.close()
    reopened = Store(path)
    assert reopened.events_for_run(run)[0]["kind"] == "snapshot"
    report = build_overview(reopened)
    assert report["runs"][0]["history_complete"] is False
    assert sum(day["pr_opened"] for day in report["daily_throughput"]) == 0
    reopened.close()
    again = Store(path)
    assert len(again.events_for_run(run)) == 1
    again.close()


def test_routes_and_escape(store, settings):
    run = make_run(store)
    with store.transaction() as conn:
        store.update_run(conn, run, pr_url="javascript:alert(1)")
    client = TestClient(create_app(settings, store))
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert 'href="javascript:' not in response.text
    assert client.get(f"/runs/{run}").status_code == 200
    assert client.get("/runs/missing").status_code == 404
    assert client.get("/reports/summary?env=invalid").status_code == 422
    assert client.get("/reports/summary?days=0").status_code == 422
    assert client.get("/reports/summary?env=live").json()["run_count"] == 0


def test_period_uses_observed_time_not_created_time(store):
    run = make_run(store)
    with store.transaction() as conn:
        store.update_run(conn, run, state="merged", pr_number=1)
        conn.execute("UPDATE run_events SET recorded_at = '2026-01-01T12:00:00+00:00'")
    report = build_overview(
        store, days=2, now=datetime(2026, 1, 3, tzinfo=timezone.utc)
    )
    assert report["runs_by_state"]["merged"] == 1
    assert sum(day["merged"] for day in report["daily_throughput"]) == 0
