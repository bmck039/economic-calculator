"""Settings that last: kept in this browser (localStorage, through the small component in
components/settings_store), so the page opens where it was left, and shareable as a link
that carries every setting (ui.share). Every widget has a key with a fixed name (ui.names).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from ui.names import VERSION, migrate
from ui.share import decode_share, encode_share, plain

_store = components.declare_component(
    "settings_store",
    path=str(Path(__file__).resolve().parent.parent / "components" / "settings_store"),
)
SETTINGS_NAME = "life-plan-settings-v1"
BACKUP_NAME = SETTINGS_NAME + "-before-shared"
PERSIST = ("w:", "k:")
SHARE_PARAM = "plan"


_DEFAULT = "_default:"  # session key prefix: the default a following setting was given
_NONE = object()


def follow(key: str, default) -> str:
    """The key of a setting whose default depends on other settings (marriage age on
    partner 1's age, home price on the buying age, ...): until the user changes it, it
    follows its default; once changed, it keeps the user's value."""
    ss = st.session_state
    last = ss.get(_DEFAULT + key, _NONE)
    if key in ss and last is not _NONE and ss[key] == last and last != default:
        del ss[key]  # untouched: the widget takes its new default
    ss[_DEFAULT + key] = default
    return key


def _persisted() -> dict:
    """What is saved and shared: every setting, except following ones still at their
    default (so they go on following it after a reload)."""
    ss = st.session_state
    return {
        k_: v_
        for k_, v_ in ss.items()
        if isinstance(k_, str)
        and k_.startswith(PERSIST)
        and plain(v_)
        and ss.get(_DEFAULT + k_, _NONE) != v_
    }


def _unpack(saved) -> dict | None:
    """Saved settings ({"v": version, "s": settings}, or version 1's bare dict) in today's
    names."""
    if not isinstance(saved, dict):
        return None
    if isinstance(saved.get("v"), int) and isinstance(saved.get("s"), dict):
        return migrate(saved["v"], saved["s"])
    return migrate(1, saved)


def _apply(saved) -> bool:
    """Replace the persisted widget values with `saved` (a dict from JSON)."""
    ss = st.session_state
    saved = _unpack(saved)
    if saved is None:
        return False
    for k_ in [k_ for k_ in ss if isinstance(k_, str) and k_.startswith(PERSIST + (_DEFAULT,))]:
        del ss[k_]
    for k_, v_ in saved.items():
        if isinstance(k_, str) and k_.startswith(PERSIST) and plain(v_):
            ss[k_] = v_
    return bool(saved)


def restore(sb):
    """Before any widget: reset, a shared link, or what this browser saved. Waits for the
    browser on the first run (st.stop) rather than showing the defaults first. Returns the
    sidebar spot for the share link (filled by finish())."""
    ss = st.session_state
    if sb.button(
        "↺ Reset all settings",
        key="_reset",
        help="Back to the defaults. Your settings are saved in this browser only.",
    ):
        for k_ in [
            k_ for k_ in ss if isinstance(k_, str) and k_.startswith(PERSIST + (_DEFAULT,))
        ]:
            del ss[k_]
        ss["_restored"], ss["_shared"] = True, False
        st.rerun()

    # ---- a shared link (?plan=...) carries every setting, compressed into the URL itself (no
    # server storage). It wins over this browser's saved settings, which are kept as a backup.
    if SHARE_PARAM in st.query_params and not ss.get("_share_seen"):
        ss["_share_seen"] = True
        shared = decode_share(st.query_params[SHARE_PARAM])
        del st.query_params[SHARE_PARAM]  # later edits are the viewer's own
        if shared is None:
            ss["_share_bad"] = True
        else:
            _apply(shared)
            ss["_restored"], ss["_shared"], ss["_backup_pending"] = True, True, True
            st.rerun()
    if ss.pop("_share_bad", False):
        sb.warning(
            "That shared link could not be read (it may have been cut short), so this "
            "page opened with your own settings."
        )

    if os.environ.get("LIFEPLAN_NO_BROWSER_STORE"):  # headless tests: no browser
        ss.setdefault("_restored", True)
    if not ss.get("_restored"):
        load_name = ss.get("_load_name", SETTINGS_NAME)
        raw = _store(mode="load", name=load_name, key=f"_settings_load_{load_name}", default=None)
        if raw is None:  # wait for the browser (it answers at once) rather than
            st.info("Loading your saved settings…")  # simulate the defaults first
            if st.button("Start with the defaults", key="_skip_restore"):
                ss["_restored"] = True
                st.rerun()
            st.stop()
        else:  # the browser answered ("" = nothing saved yet)
            try:
                saved = json.loads(raw) if raw else {}
            except ValueError:
                saved = {}
            ss["_restored"] = True
            if ss.pop("_load_name", None) is not None or isinstance(saved, dict) and saved:
                _apply(saved)
                st.rerun()
    share_box = sb.container()  # filled at the end, once every setting is known
    if ss.get("_shared"):
        sb.info("You opened a shared plan. Changes you make are saved in this browser.")
        if sb.button(
            "↩ Back to my own settings",
            key="_unshare",
            help="The settings this browser had before you opened the link.",
        ):
            ss["_shared"], ss["_restored"], ss["_load_name"] = False, False, BACKUP_NAME
            st.rerun()
    return share_box


def finish(share_box) -> None:
    """After every widget: the share link, and this run's settings saved in the browser
    (only once they were restored, so a fresh page never overwrites what was saved)."""
    ss = st.session_state
    with share_box.expander("🔗 Share this plan"):
        _code = encode_share(_persisted())
        _base = (st.context.url or "").split("?")[0].split("#")[0]
        st.caption(
            "Anyone who opens this link sees the same plan: every setting on this page "
            "is packed into the link itself, so nothing is stored on the server."
        )
        st.code(
            f"{_base}?{SHARE_PARAM}={_code}" if _base else f"?{SHARE_PARAM}={_code}",
            language=None,
            wrap_lines=True,
        )
        st.caption(
            "Copy it with the button at its top right. Changes you make after sharing "
            "are not in an earlier link."
        )

    if ss.get("_restored"):
        _store(
            mode="save",
            name=SETTINGS_NAME,
            key="_settings_save",
            default=None,
            data=json.dumps({"v": VERSION, "s": _persisted()}, sort_keys=True),
            backup=BACKUP_NAME if ss.pop("_backup_pending", False) else "",
        )
