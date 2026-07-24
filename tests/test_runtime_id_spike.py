"""Spike: verify D3's two hard technical assumptions (spec R1/R2).

Opt-in e2e (real desktop). Skips unless ``AGENT_WINDOWS_E2E=1`` is set, so the
default ``pytest -q`` run stays side-effect-free. Run with::

    AGENT_WINDOWS_E2E=1 uv run pytest -m e2e tests/test_runtime_id_spike.py -v -s

R1: a cached UIA element exposes RuntimeId via
    ``GetCachedPropertyValue(RuntimeIdProperty)``. Some providers return
    ``ReservedNotSupported`` for cached RuntimeId; if so, D3 must fall back to
    live ``GetRuntimeId()``. Either cached-works OR live-works is an acceptable
    outcome — the test records which.

R2: ``CreatePropertyCondition(RuntimeIdProperty, <rid>)`` + ``FindFirst`` hits
    the SAME element. R2 has TWO sub-cases, both probed here:

    * Naive ``list[int]`` path: **FAILS**. comtypes marshals a Python list as a
      ``SAFEARRAY(VARIANT)`` (vt=8204), but UIA types RuntimeIdProperty as
      ``SAFEARRAY(I4)`` (vt=8195); the type mismatch raises ``E_INVALIDARG``.
      Recorded as an ``xfail(strict=True)`` so the suite stays green while
      documenting the failure mode. If a future comtypes fixes the marshalling,
      strict=True flips this to XPASS-fail and we revisit.
    * Typed-SAFEARRAY workaround: **PASSES**. Building a typed
      ``_midlSAFEARRAY(c_long)`` and wrapping it in a ``VARIANT`` auto-sets
      ``vt = VT_I4 | VT_ARRAY = 8195``; ``FindFirst`` then returns the same
      element (RuntimeId match confirmed). This is the path D3 ships in a
      ``create_runtime_id_condition`` helper — the design is viable.

This spike GATES Tasks 2-5. It does not assert product behaviour; it reports
the R1/R2 outcome via pytest pass/skip/fail + stdout diagnostics.
"""
import ctypes
import os
from _ctypes import COMError

import pytest

pytestmark = pytest.mark.e2e


def _e2e_enabled() -> bool:
    """Mirror test_uia_bypass_e2e.py / test_e2e_daemon.py opt-in gate."""
    return bool(os.environ.get("AGENT_WINDOWS_E2E"))


@pytest.fixture(autouse=True)
def _com_sta_init():
    """Init COM STA on the test thread before any UIA call.

    The daemon runs all UIA/COM on a dedicated STA worker
    (``pythoncom.CoInitialize`` in daemon/server.py). ``_AutomationClient``
    requires its first caller to have initialized COM. We init STA here to
    match the production apartment — an MTA client could spuriously fail R2 on
    apartment-marshalling grounds, which would corrupt the spike result.
    """
    if not _e2e_enabled():
        yield
        return
    import comtypes

    comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
    try:
        yield
    finally:
        comtypes.CoUninitialize()


def _pick_real_interactive_element():
    """Snapshot the foreground window, return a tuple for the first element
    that exposes a usable RuntimeId.

    Walks the foreground window's cached subtree (one
    ``BuildUpdatedCache(Subtree)`` call) depth-first, preferring an interactive
    ControlType (Button/Edit/Text/ListItem). If none is found, falls back to
    the first element — including the window root — whose live
    ``GetRuntimeId()`` is non-empty. The foreground window is used so the spike
    is deterministic on any host with a visible desktop.

    Returns ``(handle, cached_control, rid_cached, rid_live)`` where
    ``rid_cached`` is the value read from the cache (may be ``None`` or a
    not-supported sentinel) and ``rid_live`` is from ``GetRuntimeId()``.
    """
    from agent_windows.uia.controls import ControlFromHandle
    from agent_windows.uia.core import CreateCacheRequest
    from agent_windows.uia.enums import ControlType, PropertyId, TreeScope

    fg = ctypes.windll.user32.GetForegroundWindow()
    assert fg, "no foreground window on the test host"

    # Cache RuntimeId (R1) plus the properties used to identify/confirm the
    # element. TreeScope_Subtree so a single BuildUpdatedCache pre-populates the
    # whole window tree and GetCachedChildren recurses cheaply.
    cr = CreateCacheRequest()
    cr.TreeScope = TreeScope.TreeScope_Subtree
    for pid in (
        PropertyId.NameProperty,
        PropertyId.ControlTypeProperty,
        PropertyId.RuntimeIdProperty,
        PropertyId.BoundingRectangleProperty,
        PropertyId.IsEnabledProperty,
    ):
        cr.AddProperty(pid)

    root = ControlFromHandle(fg)
    assert root is not None, "ControlFromHandle(foreground) returned None"
    cached_root = root.BuildUpdatedCache(cr)

    interactive_types = {
        ControlType.ButtonControl,
        ControlType.EditControl,
        ControlType.TextControl,
        ControlType.ListItemControl,
    }

    fallback = None  # (handle, control, rid_cached, rid_live) if no interactive
    stack = [cached_root]
    seen = 0
    while stack and seen < 5000:
        node = stack.pop()
        seen += 1
        try:
            ct = node.GetCachedPropertyValue(PropertyId.ControlTypeProperty)
        except Exception:
            ct = None
        # Control.GetCachedPropertyValue swallows COMError -> None. Probe live
        # rid regardless: R1's fallback path needs it, and it selects the
        # fallback candidate.
        try:
            rid_live = node.GetRuntimeId()
        except Exception:
            rid_live = None
        rid_cached = node.GetCachedPropertyValue(PropertyId.RuntimeIdProperty)

        if rid_live and fallback is None:
            fallback = (fg, node, rid_cached, rid_live)

        if ct in interactive_types and rid_live:
            return fg, node, rid_cached, rid_live

        try:
            children = node.GetCachedChildren() or []
        except Exception:
            children = []
        stack.extend(children)

    if fallback is not None:
        return fallback
    pytest.skip("no element with a RuntimeId found in the foreground window")


def test_r1_runtime_id_cacheable():
    """R1: GetCachedPropertyValue(RuntimeIdProperty) returns a usable value for
    at least one real element. If cached is None/sentinel but live works, R1 is
    "live-only" — still a pass, but D3 must use live GetRuntimeId() everywhere.
    """
    if not _e2e_enabled():
        pytest.skip("Set AGENT_WINDOWS_E2E=1 to run the D3 RuntimeId spike")

    from agent_windows.uia.enums import PropertyId

    fg, node, rid_cached, rid_live = _pick_real_interactive_element()

    ct = node.GetCachedPropertyValue(PropertyId.ControlTypeProperty)
    print("\nR1 probe: control_type={!r} name={!r}".format(ct, node.Name))
    print("R1 result: cached={!r} live={!r}".format(rid_cached, rid_live))

    assert rid_live, (
        "R1 NEGATIVE: live GetRuntimeId returned empty — element has no "
        "RuntimeId at all. Neither cached nor live works; D3 cannot use "
        "RuntimeId for this element."
    )
    if not rid_cached:
        pytest.skip(
            "R1 live-only: provider does not surface a cached RuntimeId (got "
            "{!r}). D3 falls back to live GetRuntimeId(). Documented; not a "
            "failure.".format(rid_cached)
        )


@pytest.mark.xfail(
    strict=True,
    raises=COMError,
    reason=(
        "comtypes marshals Python list[int] as SAFEARRAY(VARIANT) (vt=8204); "
        "UIA RuntimeIdProperty is typed SAFEARRAY(I4) (vt=8195). The mismatch "
        "raises E_INVALIDARG. D3 must build a typed _midlSAFEARRAY(c_long) "
        "instead — see test_r2_findfirst_by_runtimeid_typed_safearray."
    ),
)
def test_r2_findfirst_by_runtimeid_list_int_fails():
    """R2 naive baseline: ``CreatePropertyCondition(RuntimeId, list[int])``
    raises ``E_INVALIDARG``. This is the failure mode D3's helper must avoid.

    ``xfail(strict=True)`` documents the failure without going red; if a future
    comtypes release fixes list->SAFEARRAY(I4) marshalling, this flips to
    XPASS-strict-fail and we revisit.
    """
    if not _e2e_enabled():
        pytest.skip("Set AGENT_WINDOWS_E2E=1 to run the D3 RuntimeId spike")

    from agent_windows.uia.controls import ControlFromHandle
    from agent_windows.uia.core import CreatePropertyCondition
    from agent_windows.uia.enums import PropertyId, TreeScope

    fg, node, rid_cached, rid_live = _pick_real_interactive_element()
    assert rid_live, "no live RuntimeId to build a PropertyCondition from"

    rid = list(rid_live)
    root = ControlFromHandle(fg)
    assert root is not None, "ControlFromHandle(foreground) returned None"

    # This call raises COMError(E_INVALIDARG). Wrapped in xfail above so the
    # suite records the failure mode cleanly.
    cond = CreatePropertyCondition(PropertyId.RuntimeIdProperty, rid)
    found = root.FindFirst(TreeScope.TreeScope_Subtree, cond)
    # If comtypes ever marshals the list correctly, FindFirst should hit the
    # same element; assert that so an XPASS is meaningful, not spurious.
    assert found is not None
    assert list(found.GetRuntimeId()) == rid


def test_r2_findfirst_by_runtimeid_typed_safearray():
    """R2 workaround (the path D3 ships): build a typed ``SAFEARRAY(INT)`` via
    ``_midlSAFEARRAY(c_long)``, wrap in a ``VARIANT`` (auto-sets
    ``vt = VT_I4 | VT_ARRAY``), and ``FindFirst`` hits the SAME element.

    Validates that comtypes can produce the SAFEARRAY(I4) VARIANT UIA expects,
    and that ``FindFirst(Subtree, PropertyCondition(RuntimeId, rid))`` is an
    identity lookup independent of visual z-order.
    """
    if not _e2e_enabled():
        pytest.skip("Set AGENT_WINDOWS_E2E=1 to run the D3 RuntimeId spike")

    from comtypes.automation import VT_ARRAY, VT_I4, VARIANT, _midlSAFEARRAY

    from agent_windows.uia.controls import ControlFromHandle
    from agent_windows.uia.core import _AutomationClient
    from agent_windows.uia.enums import PropertyId, TreeScope

    fg, node, rid_cached, rid_live = _pick_real_interactive_element()
    assert rid_live, "no live RuntimeId to build a PropertyCondition from"

    rid = list(rid_live)
    root = ControlFromHandle(fg)
    assert root is not None, "ControlFromHandle(foreground) returned None"

    # Build a SAFEARRAY(I4): _midlSAFEARRAY(c_long) is comtypes' typed safearray
    # constructor for [in] SAFEARRAY(long) IDL parameters. Wrapping it in a
    # VARIANT auto-sets vt = VT_I4 | VT_ARRAY (8195), which is what UIA's
    # RuntimeIdProperty (a SAFEARRAY(int)) requires.
    sa_int = _midlSAFEARRAY(ctypes.c_long)
    safearray = sa_int.from_param(rid)
    v = VARIANT()
    v.value = safearray
    print("\nR2 workaround: VARIANT.vt={} (want {}={})".format(
        v.vt, VT_I4 | VT_ARRAY, 0x2003))

    ia = _AutomationClient.instance().IUIAutomation
    cond = ia.CreatePropertyCondition(PropertyId.RuntimeIdProperty, v)
    found = root.FindFirst(TreeScope.TreeScope_Subtree, cond)

    print("R2 result: found={!r} found_name={!r}".format(
        found, found.Name if found else None))

    assert found is not None, (
        "R2 WORKAROUND FAILED: FindFirst by RuntimeId(typed safearray) "
        "returned None. The typed-safearray path is D3's only viable option — "
        "escalate before Tasks 2-5."
    )
    found_rid = list(found.GetRuntimeId())
    assert found_rid == rid, (
        "R2 IDENTITY MISMATCH: found RuntimeId {!r} != query {!r}.".format(
            found_rid, rid)
    )
    print("R2 PASS: FindFirst returned the same element (rid match confirmed)")
