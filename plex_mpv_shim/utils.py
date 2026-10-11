import certifi
import logging
import os
import requests
import urllib.request, urllib.parse, urllib.error
import socket
import ipaddress
import uuid
import re
import sys
import ssl

from .conf import settings
from datetime import datetime
from functools import wraps

PLEX_TOKEN_RE = re.compile("(token|X-Plex-Token)=[^&]*")

log = logging.getLogger("utils")
plex_eph_tokens = {}
plex_sessions = {}
plex_transcode_sessions = {}

class Timer(object):
    def __init__(self):
        self.restart()

    def restart(self):
        self.started = datetime.now()

    def elapsedMs(self):
        return  self.elapsed() * 1e3

    def elapsed(self):
        return (datetime.now()-self.started).total_seconds()

def synchronous(tlockname):
    """
    A decorator to place an instance based lock around a method.
    From: http://code.activestate.com/recipes/577105-synchronization-decorator-for-class-methods/
    """

    def _synched(func):
        @wraps(func)
        def _synchronizer(self,*args, **kwargs):
            tlock = self.__getattribute__( tlockname)
            tlock.acquire()
            try:
                return func(self, *args, **kwargs)
            finally:
                tlock.release()
        return _synchronizer
    return _synched

def upd_token(domain, token):
    plex_eph_tokens[domain] = token

def get_transcode_session(domain, create=True):
    if domain not in plex_transcode_sessions:
        if not create:
            return
        session = str(uuid.uuid4())
        plex_transcode_sessions[domain] = session
    return plex_transcode_sessions[domain]

def clear_transcode_session(domain):
    if domain in plex_transcode_sessions:
        del plex_transcode_sessions[domain]

def get_session(domain):
    if domain not in plex_sessions:
        session = str(uuid.uuid4())
        plex_sessions[domain] = session
    return plex_sessions[domain]

def normalize_plex_url(url):
    """
    Rewrites Plex server URLs to ``http://`` when the ``force_http`` setting
    is enabled. This allows connecting to servers without a valid TLS
    certificate without having to deal with certificate errors.
    """
    if settings.force_http and url.lower().startswith("https://"):
        return "http://" + url[len("https://"):]
    return url

def get_plex_url(url, data=None, quiet=False):
    if not data:
        data = {}

    url = normalize_plex_url(url)
    parsed_url = urllib.parse.urlsplit(url)
    domain = parsed_url.hostname

    if parsed_url.scheme != "https" and not (settings.allow_http or settings.force_http):
        raise ValueError("HTTP is not enabled in the configuration.")

    if domain in plex_eph_tokens:
        data.update({
            "X-Plex-Token": plex_eph_tokens[domain]
        })
    else:
        log.error("get_plex_url No token for: %s" % domain)

    data.update({
        "X-Plex-Version":             "2.0",
        "X-Plex-Client-Identifier":   settings.client_uuid,
        "X-Plex-Provides":            "player",
        "X-Plex-Device-Name":         settings.player_name,
        "X-Plex-Model":               "RaspberryPI",
        "X-Plex-Device":              "RaspberryPI",
        "X-Plex-Session-Identifier":  get_session(domain),

        # Lies
        "X-Plex-Product":             "Plex MPV Shim",
        "X-Plex-Platform":            "Plex Home Theater",
        "X-Plex-Client-Profile-Name": settings.client_profile,
    })

    # Kinda ghetto...
    sep = "?"
    if sep in url:
        sep = "&"

    if data:
        url = "%s%s%s" % (url, sep, urllib.parse.urlencode(data))

    if not quiet:
        log.debug("get_plex_url Created URL: %s" % sanitize_msg(url))

    return url

_ssl_context = None
_requests_session = None

def get_ssl_context():
    """
    Returns the shared SSL context used for all HTTPS connections.

    The context is built once from certifi's CA bundle. This is also the
    Python 3.13-compatible replacement for the ``cafile``/``capath``
    arguments that were removed from ``urllib.request.urlopen``.
    """
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context(cafile=certifi.where())
    return _ssl_context

def get_requests_session():
    """
    Returns the shared ``requests`` session used for Plex API calls.

    The session verifies against certifi's CA bundle so that it matches the
    SSL context used by :func:`urlopen`.
    """
    global _requests_session
    if _requests_session is None:
        _requests_session = requests.Session()
        _requests_session.verify = certifi.where()
    return _requests_session

def urlopen(url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """
    Opens a URL using the shared certifi SSL context for HTTPS requests.

    This is the single place the application performs ``urllib`` requests,
    keeping the Python 3.13 SSL context handling consistent everywhere.
    """
    kwargs = {"timeout": timeout}
    if url.lower().startswith("https://"):
        kwargs["context"] = get_ssl_context()
    return urllib.request.urlopen(url, data=data, **kwargs)

def plex_urlopen(url, data=None, quiet=False, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """
    Builds the full Plex API URL (including the token) and opens it.
    """
    return urlopen(get_plex_url(url, data, quiet), timeout=timeout)

def safe_urlopen(url, data=None, quiet=False):
    """
    Opens a url and returns True if an HTTP 200 code is returned,
    otherwise returns False.
    """
    try:
        page = plex_urlopen(url, data, quiet)
        if page.code == 200:
            return True
        log.error("Error opening URL '%s': page returned %d" % (sanitize_msg(url),
                                                                page.code))
    except Exception as e:
        log.error("Error opening URL '%s':  %s" % (sanitize_msg(url), e))

    return False

def is_local_domain(domain):
    try:
        return ipaddress.ip_address(socket.gethostbyname(domain)).is_private
    except socket.gaierror as e:
        if e.errno == socket.EAI_NODATA:
            try:
                # try checking for an IPv6 address
                return ipaddress.ip_address(socket.getaddrinfo(domain, None, socket.AF_INET6)[0][4][0]).is_private
            except socket.gaierror:
                log.warning("Unable to check local/remote for domain (IPv6): %s" % domain, exc_info=True)
        else:
            log.warning("Unable to check local/remote for domain: %s" % domain, exc_info=True)
        return False

def sanitize_msg(text):
    if settings.sanitize_output:
        return re.sub(PLEX_TOKEN_RE, "\\1=REDACTED", text)
    return text

def mpv_color_to_plex(color):
    return '#'+color.lower()[3:]

def plex_color_to_mpv(color):
    return '#FF'+color.upper()[1:]

def get_resource(*path):
    # Detect if bundled via pyinstaller.
    # From: https://stackoverflow.com/questions/404744/
    if getattr(sys, '_MEIPASS', False):
        application_path = os.path.join(sys._MEIPASS, "plex_mpv_shim")
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(application_path, *path)
