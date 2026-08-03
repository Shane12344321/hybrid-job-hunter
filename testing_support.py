"""Shared helpers for the offline suites.

The suites are offline by convention, but nothing used to enforce it. That
matters more than it sounds: when `probe_candidate` gained a derived-domain
fallback, three tests that stubbed only `probe_name_detailed` began issuing
real HTTP requests and still passed — just 500x slower. A test that silently
reaches the network stops testing the code and starts testing the internet,
and it will pass or fail for reasons the diff never explains.

`block_network` makes that failure mode loud: any call a test has not stubbed
raises immediately. Explicit `mock.patch.object(...)` stubs shadow the block,
so intentional fakes keep working.
"""
import requests

_ORIGINALS = {}


def _refuse(*args, **kwargs):
    raise AssertionError(
        "offline test made a real network call; stub it with mock.patch")


def block_network():
    """Point requests' verbs at a raiser. Call from setUpModule()."""
    for attr in ("get", "post", "put", "delete", "head", "patch", "request"):
        if not hasattr(requests, attr) or attr in _ORIGINALS:
            continue
        _ORIGINALS[attr] = getattr(requests, attr)
        setattr(requests, attr, _refuse)


def restore_network():
    """Undo block_network(). Call from tearDownModule()."""
    for attr, original in _ORIGINALS.items():
        setattr(requests, attr, original)
    _ORIGINALS.clear()
