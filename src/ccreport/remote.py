"""Fetching a report from a ccreport server, for `ccreport --server URL`.

The rows arrive already folded — the server ran the same `aggregate.py` the
local path runs — so all that happens here is a request, a parse, and the
renderers in ccreport.py.

There is deliberately no fallback to the local cache. A merged report and a
single-machine report differ by exactly the thing being asked for, so a server
that cannot be reached is an error and not a quieter answer.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TIMEOUT_S = 30
"""Long enough for a server folding a large corpus, short enough that a
mistyped host fails while the person is still watching."""


class RemoteError(Exception):
    """The server could not be reached, or would not answer with a report."""


def _url(base: str, kind: str, params: dict[str, Any]) -> str:
    query = {k: v for k, v in params.items() if v not in (None, False, "")}
    encoded = urllib.parse.urlencode({k: str(v) for k, v in query.items()})
    return f"{base.rstrip('/')}/v1/report/{kind}" + (f"?{encoded}" if encoded else "")


def fetch_health(base: str, token: str) -> dict:
    """Validate a token against a server, and learn what it belongs to.

    Called by `ccreport server connect` before it writes anything, so a
    mistyped token fails while the person is still looking at it rather than
    silently at a background push half an hour later.

    Raises:
        RemoteError: the server refused the token or could not be reached.
    """
    url = f"{base.rstrip('/')}/v1/health"
    request = urllib.request.Request(  # noqa: S310
        url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RemoteError(f"{base} refused that token") from exc
        raise RemoteError(f"{url} answered {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RemoteError(f"{url} could not be reached: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RemoteError(f"{url} did not answer with JSON") from exc


def fetch_report(base: str, kind: str, **params: Any) -> dict:
    """One report from *base*, as the object the server sent.

    Raises:
        RemoteError: the request failed, the server refused it, or the reply
            was not JSON. The message names the URL that was tried, because
            the first thing to check is whether it is the URL that was meant.
    """
    url = _url(base, kind, params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RemoteError(f"{url} answered {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RemoteError(f"{url} could not be reached: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RemoteError(f"{url} could not be reached: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RemoteError(f"{url} did not answer with JSON") from exc
