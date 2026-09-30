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
"""Compare devin/security-scan/profile.json with the live Devin scan profile.

The Devin API has no endpoint for editing a code scan profile, so the file is
kept in step by hand: change the profile in Devin (it goes through an approval
card), then run this with ``--pull`` and commit the result. Without
``--pull`` it exits 1 and names each field that differs.

Needs DEVIN_CODE_SCANS_API_TOKEN: a service user with ViewCodeScans.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "devin" / "security-scan" / "profile.json"
API = "https://api.devin.ai/v3"
UNMANAGED_FIELDS = frozenset({"created_at"})


def managed(profile: Mapping[str, object]) -> dict[str, object]:
    return {k: v for k, v in profile.items() if k not in UNMANAGED_FIELDS}


def diff_profile(
    expected: Mapping[str, object], live: Mapping[str, object]
) -> list[str]:
    """Names of the fields whose values differ, in sorted order."""
    want, have = managed(expected), managed(live)
    return sorted(k for k in want.keys() | have.keys() if want.get(k) != have.get(k))


def fetch_profile(org_id: str, profile_id: str, token: str) -> dict[str, object]:
    url = f"{API}/organizations/{org_id}/code-scans/profiles/{profile_id}"
    resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    data: dict[str, object] = resp.json()
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pull",
        action="store_true",
        help="overwrite profile.json with the live profile",
    )
    args = parser.parse_args()

    token = os.environ.get("DEVIN_CODE_SCANS_API_TOKEN")
    if not token:
        print("DEVIN_CODE_SCANS_API_TOKEN is not set", file=sys.stderr)
        return 2

    expected = json.loads(PROFILE_PATH.read_text())
    live = fetch_profile(str(expected["org_id"]), str(expected["profile_id"]), token)

    if args.pull:
        PROFILE_PATH.write_text(json.dumps(managed(live), indent=2) + "\n")
        print(f"wrote {PROFILE_PATH.relative_to(ROOT)}")
        return 0

    changed = diff_profile(expected, live)
    if changed:
        print("profile.json differs from the live profile in: " + ", ".join(changed))
        return 1
    print("profile.json matches the live profile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
