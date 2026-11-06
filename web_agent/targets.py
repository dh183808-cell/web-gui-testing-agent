"""Dynamic target configuration. One exact HTTP(S) origin per run.

No hostname wildcards or implicit internet access. Fixture contexts are owned by
Pipeline so the HTTP service survives planning, browser execution and reporting.
"""
from contextlib import contextmanager
from pathlib import Path
import json
from urllib.parse import urlsplit
import ipaddress

from .dynamic_execution import origin
from .dynamic_schema import TestDesign

FIXTURE_PATHS = {'catalog': '/search', 'form': '/apply', 'download': '/download'}


def is_loopback(url):
    host = urlsplit(url).hostname
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


def target_metadata(url):
    return {'start_url': url, 'allowed_origins': [origin(url)],
            'scope': 'loopback' if is_loopback(url) else 'external',
            'navigation_policy': 'exact_same_origin'}


@contextmanager
def dynamic_target(*, target_url=None, fixture_site=None):
    if fixture_site is not None:
        if fixture_site not in FIXTURE_PATHS or target_url is not None:
            raise ValueError('Choose one built-in fixture or one explicit start URL')
        from .dynamic_fixture import dynamic_fixture
        with dynamic_fixture() as (base, _):
            yield base + FIXTURE_PATHS[fixture_site]
    else:
        origin(target_url)  # strict URL validation without navigation
        yield target_url


def load_template(path):
    document = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if type(document) is not dict:
        raise ValueError('Template must be a TestDesign JSON object')
    return document


def bind_template(document, *, requirement, target_url):
    """Bind ONLY the explicit portable URL placeholder; never rewrite semantics."""
    if isinstance(document, TestDesign):
        return document
    if type(document) is not dict:
        raise ValueError('Dynamic template mode requires an authored TestDesign')
    from copy import deepcopy
    result = deepcopy(document)
    if result.get('target_url') == '$TARGET_URL':
        result['target_url'] = target_url
    if result.get('requirement') != requirement:
        raise ValueError('Template requirement must exactly match the submitted requirement')
    return result
