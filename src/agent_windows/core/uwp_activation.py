"""UWP app activation via IApplicationActivationManager (real PID at launch).

Replaces the ``explorer.exe shell:AppsFolder`` surrogate launch in
``app_manager``, which could not return the app's real process id. The
Application Activation Manager COM API activates by AppUserModelID (AUMID) and
hands back the activated process id directly.

Must be called from the daemon's COM STA worker thread (COM is apartment-bound
there; see ``uia/core.py`` _AutomationClient note). Heavy imports (comtypes)
stay in this daemon-side module, never on the CLI cold-start path.
"""

import ctypes

import comtypes
from comtypes import GUID, IUnknown
from _ctypes import COMError

# {45BA127D-10A8-46EA-8AB7-56EA9078943C} (CApplicationActivationManager, twinui.appcore.dll)
CLSID_ApplicationActivationManager = "{45BA127D-10A8-46EA-8AB7-56EA9078943C}"
# {2E941141-7F97-4756-BA1D-9DECDE894A3D} (MIDL_INTERFACE in shobjidl_core.h)
IID_IApplicationActivationManager = "{2E941141-7F97-4756-BA1D-9DECDE894A3D}"
AO_NONE = 0  # ACTIVATEOPTIONS: normal launch


class IApplicationActivationManager(IUnknown):
    """Minimal vtable: ActivateApplication is the first method after IUnknown's
    three (vtable slot 3), so declaring only it is positional-safe. The other
    two (ActivateForFile / ActivateForProtocol) are unused and omitted."""

    _iid_ = GUID(IID_IApplicationActivationManager)
    _methods_ = [
        comtypes.STDMETHOD(
            ctypes.HRESULT, "ActivateApplication",
            [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
             ctypes.POINTER(ctypes.c_ulong)],
        ),
    ]


def _create_activation_manager() -> IApplicationActivationManager:
    """CoCreate the real activation manager (daemon STA thread)."""
    return comtypes.CoCreateInstance(
        GUID(CLSID_ApplicationActivationManager),
        interface=IApplicationActivationManager,
    )


def activate_uwp_app(
    aumid: str, args: str = "", *, _create=None,
) -> int:
    """Activate a UWP app by AUMID and return its real process id.

    ``args`` is passed as the activation command line (ignored by most UWP apps
    that don't implement Launch-activated args). ``_create`` is a DI seam for
    tests (defaults to the real CoCreate). On activation failure the COMError is
    converted to ValueError (-> INVALID_PARAMS via route_request) so the bare
    HRESULT never reaches the JSON-RPC surface.
    """
    # TODO(P5): guard against indefinite block if the activation broker hangs
    # (e.g. suspended app that fails to resume).  A watchdog thread or
    # CoWaitForMultipleHandles timeout would keep the daemon STA responsive.
    try:
        mgr = (_create if _create is not None else _create_activation_manager)()
        pid = ctypes.c_ulong(0)
        mgr.ActivateApplication(aumid, args, AO_NONE, ctypes.byref(pid))
        return int(pid.value)
    except COMError as ex:
        raise ValueError(
            f"UWP activation failed for {aumid}: {ex.hresult:#x}"
        ) from ex
