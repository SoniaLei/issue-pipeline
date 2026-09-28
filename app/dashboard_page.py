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
"""The dashboard page: a single self-contained HTML document.

No build step, no CDN, no framework. ``static/dashboard.html`` fetches
``/api/dashboard`` for the overview and table and ``/api/tasks/{id}/timeline``
when a row is opened, and renders everything client-side with ``textContent``
so nothing from GitHub or a session's structured output is ever interpreted
as markup.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

_PAGE_PATH = Path(__file__).with_name("static") / "dashboard.html"


@cache
def _template() -> str:
    return _PAGE_PATH.read_text(encoding="utf-8")


def render_page(*, default_env: str) -> str:
    return _template().replace("__DEFAULT_ENV__", json.dumps(default_env))
