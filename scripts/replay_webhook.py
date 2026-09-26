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
"""Sign a fixture and POST it to a running API.

The quickest way to confirm signature verification is actually on: change one
byte of the body and the same command gets a 401.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", help="path to a webhook payload JSON file")
    parser.add_argument("--event", default="issues", help="X-GitHub-Event value")
    parser.add_argument("--url", default="http://localhost:8000/webhooks/github")
    parser.add_argument("--delivery", default=None, help="X-GitHub-Delivery value")
    args = parser.parse_args()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print("GITHUB_WEBHOOK_SECRET is not set", file=sys.stderr)
        return 2

    path = Path(args.fixture)
    if not path.exists():
        path = ROOT / "fixtures" / args.fixture
    body = path.read_bytes()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    response = httpx.post(
        args.url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": args.event,
            "X-GitHub-Delivery": args.delivery or str(uuid.uuid4()),
        },
        timeout=10.0,
    )
    print(response.status_code, response.text)
    return 0 if response.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
