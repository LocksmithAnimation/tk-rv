#
# Copyright (C) 2023  Autodesk, Inc. All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0
#

from typing import Dict, Iterable
from urllib.parse import urlparse


def _session_token_for_url(url: str) -> str:
    """
    Returns the session token for the given url.
    """

    # location of sgtk can change based on the execution of RV
    from sgtk.authentication import session_cache

    user = session_cache.get_current_user(url)
    if not user:
        return ""

    session_data = session_cache.get_session_data(url, user)
    if not session_data:
        return ""

    return session_data.get("session_token", "")


def is_plugin_enabled() -> bool:
    """
    Returns True if the media library plugin is enabled.
    """
    try:
        import sgtk

        return True
    except ImportError:
        return False


def is_library_media_url(url: str) -> bool:
    """
    Returns True if the given url is a url that should be handled by the media library.
    """
    incoming_url = urlparse(url)
    if incoming_url.scheme == "":
        return False

    return _session_token_for_url(url) != ""


def is_streaming(url: str) -> bool:
    """
    Returns True if the given url is a streaming url and should receive cookies and headers.
    """

    return True


def is_redirecting(url: str) -> bool:
    """
    Returns True if the given url will get redirected by the media library.
    """

    return False


def get_http_cookies(url: str) -> Iterable[Dict] or None:
    """
    Returns a list of cookies to be used for the given url.

    The cookies are returned as a list of dictionaries with the following keys:
        name: The name of the cookie.
        value: The value of the cookie.
        domain: The domain of the cookie.
        path: The path of the cookie.

    This is pulling the session id from the current user and setting it as a cookie.
    """

    if is_library_media_url(url):
        yield {
            "name": "_session_id",
            "value": _session_token_for_url(url),
            "domain": urlparse(url).netloc,
            "path": "/",
        }
    else:
        yield from ()


def get_http_headers(url: str) -> Iterable[Dict]:
    """
    Returns a list of headers to be used for the given url.

    The headers are returned as a list of dictionaries with the following keys:
        name: The name of the header.
        value: The value of the header.

    In this demonstration function, the headers are set for all urls. However, the
    headers can be set for specific urls by using the url parameter.
    """

    yield from ()


def get_http_redirection(url: str) -> str:
    """
    Returns the url to redirect to for the given url.

    """

    return url
