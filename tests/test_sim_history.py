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

from app.dashboard import build_dashboard
from app.sim_history import seed_history
from app.store import Store

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def seeded(seed: int = 7) -> Store:
    store = Store(":memory:")
    seed_history(store, days=90, seed=seed, now=NOW)
    return store


def test_ninety_days_of_history_has_ups_and_downs() -> None:
    board = build_dashboard(seeded(), "sim", now=NOW, days=90)
    daily = [day["verified"] + day["merged"] for day in board["throughput"]]
    assert len(daily) == 90
    assert 0 in daily
    assert max(daily) >= 4
    assert len(set(daily)) >= 4
    assert sum(daily[-30:]) > sum(daily[:30])

    results = board["results"]
    assert results["merged"] > 0
    assert results["closed_unmerged"] > 0
    assert board["workload"]["failed"] > 0
    assert board["workload"]["blocked"] > 0
    assert board["cost"]["acus_total"]
    assert board["speed"]["review_ready"]["samples"] > 0
    assert len(board["repos_available"]) == 2


def test_history_is_sim_only_and_never_in_the_future() -> None:
    store = seeded()
    assert build_dashboard(store, "live", now=NOW)["totals"]["runs"] == 0
    assert all(
        datetime.fromisoformat(str(event["at"])) <= NOW for event in store.all_events()
    )


def test_history_is_deterministic_per_seed() -> None:
    first = build_dashboard(seeded(), "sim", now=NOW, days=90)["throughput"]
    again = build_dashboard(seeded(), "sim", now=NOW, days=90)["throughput"]
    assert first == again
