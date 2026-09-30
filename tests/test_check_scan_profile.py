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
"""The committed security scan profile and the drift check against Devin."""

from __future__ import annotations

import json

from scripts.check_scan_profile import diff_profile, PROFILE_PATH


def load() -> dict[str, object]:
    profile: dict[str, object] = json.loads(PROFILE_PATH.read_text())
    return profile


def test_committed_profile_is_the_org_security_profile():
    profile = load()
    assert profile["profile_id"] == "csprof-2f17d866c09f4abb98cbc8fa2b0360dc"
    assert profile["scan_type"] == "security"
    assert profile["mode"] == "discover"
    assert profile["visibility"] == "org"
    assert "created_at" not in profile


def test_identical_profiles_do_not_differ_and_created_at_is_ignored():
    profile = load()
    assert diff_profile(profile, {**profile, "created_at": 1790000000}) == []


def test_changed_and_missing_fields_are_named():
    profile = load()
    live = {**profile, "exclude_globs": ["**/node_modules/**"], "triage_guidance": "x"}
    del live["report_guidance"]
    assert diff_profile(profile, live) == [
        "exclude_globs",
        "report_guidance",
        "triage_guidance",
    ]
