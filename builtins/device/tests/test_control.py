"""Screen control: the gate, the coordinate maths and the drawn indicator.

These tests never take over the real desktop. The session is built with the
indicator and the cursor switched off, and the drawing is checked as images
rather than on screen. The live behaviour -- a window on every monitor, the
system cursor, real keystrokes -- is opt-in through ``CONTROL_LIVE_TESTS=1``,
because a test suite that grabs the mouse is a test suite nobody runs twice.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from conftest import device_server
from src.control import ControlError, ControlOptions, ControlSession
from src.control.session import _has_content as _has_content_of
from src.control import capture as capture_mod
from src.settings import ControlConf
from src.control import cursor as cursor_mod
from src.control import inputs
from src.control.overlay import _banner, _edge_mask, _glow_mask, render
from src.control.screens import Screen
from src.server import DeviceServer, _do_keyboard, _do_mouse

LIVE = os.getenv("CONTROL_LIVE_TESTS") == "1"
SCREEN = Screen(0, "TEST", 0, 0, 400, 300, 0, 0, 400, 300, True, 1.0, 0)


def quiet_session(tmp_path: Path, **options) -> ControlSession:
    """A session that gates tools without touching the screen or the pointer."""
    options.setdefault("show_indicator", False)
    options.setdefault("change_cursor", False)
    return ControlSession(ControlOptions(**options), marker=tmp_path / "cursors-replaced")


# -- the gate ---------------------------------------------------------------------------

def test_control_tools_refuse_until_control_is_started(tmp_path: Path) -> None:
    session = quiet_session(tmp_path)
    with pytest.raises(ControlError) as raised:
        session.require("screenshot")
    assert "start_control" in str(raised.value)

    session.start("checking the gate")
    assert session.require("screenshot").actions == 1
    session.stop()

    with pytest.raises(ControlError):
        session.require("mouse")


def test_status_reports_what_the_user_can_see(tmp_path: Path) -> None:
    session = quiet_session(tmp_path)
    assert session.status() == {"active": False, "indicator": "not shown", "cursor": "unchanged"}

    started = session.start("demo")
    assert started["active"] is True and started["reason"] == "demo"
    assert started["session_id"] and started["actions"] == 0

    session.require("mouse")
    assert session.status()["actions"] == 1

    assert "ESC" in session.status()["ends_when"]

    stopped = session.stop()
    assert stopped == {
        "active": False, "was_active": True, "session_id": started["session_id"],
        "stopped_because": "requested", "seconds_active": stopped["seconds_active"],
        "actions": 1, "indicator": "removed", "cursor": "restored",
    }
    assert session.stop()["was_active"] is False


def test_starting_twice_keeps_the_first_session(tmp_path: Path) -> None:
    session = quiet_session(tmp_path)
    first = session.start()
    second = session.start()
    assert second["already_active"] is True
    assert second["session_id"] == first["session_id"]
    session.stop()


def test_the_escape_key_ends_control(tmp_path: Path) -> None:
    """The label promises ESC works, so the callback must really stop the session."""
    session = quiet_session(tmp_path)
    session.start()
    session._stop_from_escape()
    assert not session.active


# -- coordinates ------------------------------------------------------------------------

def test_absolute_mapping_covers_the_virtual_desktop(monkeypatch) -> None:
    monkeypatch.setattr(inputs, "virtual_screen", lambda: (-1920, -5, 3840, 1085))
    assert inputs.to_absolute(-1920, -5) == (0, 0)
    assert inputs.to_absolute(1919, 1079) == (65535, 65535)
    # A monitor left of the primary has negative coordinates and must still map.
    middle = inputs.to_absolute(0, 537)
    assert 32000 < middle[0] < 33500


def test_mouse_rejects_coordinates_that_are_not_on_the_desktop() -> None:
    with pytest.raises(ValueError, match="outside the desktop"):
        _do_mouse("click", 999_999, 999_999, "left", 1, None, None, 0, False)


def test_mouse_argument_errors_say_what_is_missing() -> None:
    with pytest.raises(ValueError, match="both x and y"):
        _do_mouse("move", None, None, "left", 1, None, None, 0, False)
    with pytest.raises(ValueError, match="to_x and to_y"):
        _do_mouse("drag", None, None, "left", 1, None, None, 0, False)
    with pytest.raises(ValueError, match="non-zero amount"):
        _do_mouse("scroll", None, None, "left", 1, None, None, 0, False)
    with pytest.raises(ValueError, match="action must be one of"):
        _do_mouse("teleport", None, None, "left", 1, None, None, 0, False)
    with pytest.raises(ValueError, match="button must be one of"):
        _do_mouse("click", None, None, "thumb", 1, None, None, 0, False)


def test_mouse_position_reports_without_moving_anything() -> None:
    before = inputs.position()
    result = _do_mouse("position", None, None, "left", 1, None, None, 0, False)
    assert result["position"] == {"x": before[0], "y": before[1]}
    assert inputs.position() == before


def test_regions_and_screen_indexes_are_validated() -> None:
    assert capture_mod.resolve_region(region={"x": 10, "y": 20, "width": 30, "height": 40}) == (10, 20, 30, 40)
    with pytest.raises(ValueError, match="positive"):
        capture_mod.resolve_region(region={"x": 0, "y": 0, "width": 0, "height": 5})
    with pytest.raises(ValueError, match="integer x, y, width and height"):
        capture_mod.resolve_region(region={"x": 0, "y": 0})
    with pytest.raises(ValueError, match="no screen with index"):
        capture_mod.resolve_region(screen=99)


def test_capture_explains_how_to_get_back_to_screen_coordinates() -> None:
    shot = capture_mod.Capture(b"", "image/png", 1920, -5, 1920, 1080, 960, 540)
    facts = shot.to_dict()
    assert facts["scale"] == 0.5
    assert facts["to_screen_coordinates"] == (
        "screen_x = 1920 + image_x / 0.5, screen_y = -5 + image_y / 0.5"
    )


# -- keyboard ---------------------------------------------------------------------------

def test_key_chords_parse_into_modifiers_and_a_key() -> None:
    modifiers, key = inputs.parse_chord("ctrl+shift+s")
    assert [vk for vk, _ in modifiers] == [0x11, 0x10]
    assert key == (0x53, False)
    # Navigation keys must be marked extended or they send their numpad twins.
    assert inputs.parse_chord("left")[1] == (0x25, True)
    assert inputs.parse_chord("delete")[1] == (0x2E, True)
    assert inputs.parse_chord("f5")[1] == (0x74, False)
    assert inputs.parse_chord("win+d")[0][0] == (0x5B, True)
    # A character that needs shift on this layout brings shift with it.
    assert 0x10 in [vk for vk, _ in inputs.parse_chord("!")[0]]


def test_unknown_keys_are_named_in_the_error() -> None:
    with pytest.raises(ValueError, match="unknown key: 'wiggle'"):
        inputs.parse_chord("wiggle")
    with pytest.raises(ValueError, match="unknown modifier: 'hyper'"):
        inputs.parse_chord("hyper+a")
    with pytest.raises(ValueError, match="empty key chord"):
        inputs.parse_chord("")


def test_keyboard_arguments_are_checked() -> None:
    with pytest.raises(ValueError, match="type needs text"):
        _do_keyboard("type", "", "")
    with pytest.raises(ValueError, match="press needs keys"):
        _do_keyboard("press", "", "   ")
    with pytest.raises(ValueError, match="'type' or 'press'"):
        _do_keyboard("sing", "la", "")


# -- what the user sees -----------------------------------------------------------------

def test_the_glow_is_strongest_at_the_edge_and_gone_in_the_middle() -> None:
    mask = _glow_mask(400, 300, 60, 165)
    assert mask.getpixel((0, 150)) == 165
    assert mask.getpixel((30, 150)) < 165
    assert mask.getpixel((59, 150)) < mask.getpixel((30, 150))
    assert mask.getpixel((200, 150)) == 0  # the middle of the screen stays clear
    # Every edge, not just the left one.
    assert mask.getpixel((200, 0)) == 165 and mask.getpixel((399, 150)) == 165


def test_the_edge_hairline_sits_on_the_outermost_pixels() -> None:
    mask = _edge_mask(400, 300, 3)
    assert mask.getpixel((0, 150)) == mask.getpixel((2, 150)) == 225
    assert mask.getpixel((3, 150)) == 0


def test_the_banner_renders_the_label() -> None:
    banner = _banner("ChatGPT 正在控制电脑", 1.0)
    assert banner.width > 120 and banner.height > 20
    assert banner.getpixel((banner.width // 2, banner.height // 2))[3] > 200  # opaque pill
    assert banner.getpixel((0, 0))[3] < 40  # rounded corner stays transparent


def test_the_banner_tells_the_user_how_to_stop_control() -> None:
    from src.control.overlay import ESCAPE_HINT

    assert "ESC" in ESCAPE_HINT
    with_hint = _banner("ChatGPT 正在控制电脑", 1.0)
    without = _banner("ChatGPT 正在控制电脑", 1.0, hint="")
    assert with_hint.width > without.width, "the hint should occupy real space on the pill"


def test_the_overlay_bitmap_is_premultiplied_and_screen_sized() -> None:
    raw = render(SCREEN, "ChatGPT 正在控制电脑")
    assert len(raw) == SCREEN.width * SCREEN.height * 4
    # Premultiplied: no channel may exceed the alpha it was multiplied by.
    edge = raw[:4]
    assert max(edge[:3]) <= edge[3]
    centre = (SCREEN.width * (SCREEN.height // 2) + SCREEN.width // 2) * 4
    assert raw[centre:centre + 4] == b"\x00\x00\x00\x00"


def test_the_pointer_is_drawn_larger_than_the_system_default() -> None:
    pointer = cursor_mod._render()
    assert pointer.size == (cursor_mod.CURSOR_SIZE, cursor_mod.CURSOR_SIZE)
    assert cursor_mod.CURSOR_SIZE > 32
    assert pointer.getpixel(cursor_mod.HOTSPOT)[3] > 0, "the hotspot should be on the arrow"
    assert pointer.getpixel((cursor_mod.CURSOR_SIZE - 1, 0))[3] == 0


def test_a_cursor_swap_that_outlived_its_process_is_cleaned_up(tmp_path: Path) -> None:
    marker = tmp_path / "cursors-replaced"
    assert cursor_mod.restore_if_left_over(marker) is False
    marker.write_text("left over", encoding="utf-8")
    assert cursor_mod.restore_if_left_over(marker) is True
    assert not marker.exists()


# -- element references -----------------------------------------------------------------

def element(name: str, role: str = "button", rect=(10, 20, 60, 40), hwnd: int = 7,
            source: str = "uia"):
    from src.control.uia import Element

    return Element(role=role, name=name, rect=rect, window_hwnd=hwnd, control_type=50000,
                   source=source, actions=("click",) if source == "ocr" else ())


def test_refs_are_handed_out_and_reused_for_the_same_element() -> None:
    from src.control.observe import RefTable

    table = RefTable()
    first = table.put([element("确定"), element("取消", rect=(70, 20, 120, 40))])
    assert [ref for ref, _ in first] == ["ref_1", "ref_2"]

    # Reading again must not renumber what did not move: a model that just read
    # ref_1 should still be able to click ref_1.
    again = table.put([element("确定"), element("保存", rect=(130, 20, 180, 40))])
    assert again[0][0] == "ref_1"
    assert again[1][0] == "ref_3"
    assert table.get("ref_1").name == "确定"


def test_an_unknown_ref_says_how_to_get_a_real_one() -> None:
    from src.control.observe import RefTable

    table = RefTable()
    with pytest.raises(KeyError, match="read_ui"):
        table.get("ref_99")
    table.put([element("确定")])
    table.clear()
    with pytest.raises(KeyError):
        table.get("ref_1")


def test_refs_are_dropped_when_control_stops(tmp_path: Path) -> None:
    session = quiet_session(tmp_path)
    session.start()
    session.refs.put([element("确定")])
    assert len(session.refs) == 1
    session.stop()
    assert len(session.refs) == 0


def test_the_diff_says_what_appeared_and_what_went(tmp_path: Path) -> None:
    from src.control.observe import Observation, RefTable, diff

    table = RefTable()
    before = Observation("focused", "记事本", table.put([element("确定")]))
    after = Observation("focused", "记事本", table.put([
        element("确定"), element("不保存", rect=(70, 20, 130, 40)),
    ]))
    changed = diff(before, after)
    assert [item["name"] for item in changed["appeared"]] == ["不保存"]
    assert "gone" not in changed

    gone = diff(after, Observation("focused", "桌面", table.put([])))
    assert set(gone["gone"]) == {"ref_1", "ref_2"}
    assert gone["foreground_changed_from"] == "记事本"
    assert diff(before, before)["unchanged"] is True


def test_scopes_are_validated(tmp_path: Path) -> None:
    session = quiet_session(tmp_path)
    with pytest.raises(ValueError, match="unknown scope"):
        session._scope_hwnd("everything")
    with pytest.raises(ValueError, match="numeric window handle"):
        session._scope_hwnd("window:not-a-number")


def test_settling_reports_how_long_it_waited() -> None:
    from src.control.observe import settle

    result = settle(timeout=0.5)
    assert "settled" in result and 0 <= result["waited_s"] <= 1.0


# -- recognised text --------------------------------------------------------------------

def test_recognised_chinese_loses_the_engine_s_inserted_spaces() -> None:
    from src.control import ocr

    # Windows OCR reports every CJK glyph as its own word, so a line arrives as
    # "不 保 存"; the gaps are an artefact and would break name matching.
    assert ocr.clean("不 保 存") == "不保存"
    assert ocr.clean("发 送 Send") == "发送 Send"
    assert ocr.clean("  Cancel  ") == "Cancel"


def test_text_inside_a_known_control_is_deduplicated() -> None:
    from src.control import ocr

    button = element("确定", rect=(100, 100, 220, 140))
    label = element("确定", role="ocr_text", rect=(140, 110, 180, 130), source="ocr")
    fused = ocr.fuse([button], [label])
    assert [e.name for e in fused] == ["确定"], "the label describes the button, not a second target"


def test_text_inside_a_window_sized_element_survives() -> None:
    """Regression: containment against a container deleted everything.

    A chat window exposes exactly one accessibility element -- itself -- and
    every recognised line sits inside it. Applied naively, containment dropped
    all 78 lines and the window came back empty.
    """
    from src.control import ocr

    window = element("企业微信", role="window", rect=(0, 0, 1320, 838))
    lines = [
        element("搜索", role="ocr_text", rect=(526, 148, 576, 163), source="ocr"),
        element("发送", role="ocr_text", rect=(1200, 800, 1260, 820), source="ocr"),
    ]
    fused = ocr.fuse([window], lines)
    assert [e.name for e in fused] == ["企业微信", "搜索", "发送"]
    assert [e.source for e in fused] == ["uia", "ocr", "ocr"]


def test_recognised_text_can_only_be_clicked(tmp_path: Path) -> None:
    from src.server import _do_element

    session = quiet_session(tmp_path)
    session.start()
    text = element("发送", role="ocr_text", rect=(10, 10, 60, 30), source="ocr")
    with pytest.raises(ValueError, match="recognised from the screen by OCR"):
        _do_element(session, text, "invoke", "")
    session.stop()


def test_a_detected_icon_can_only_be_clicked_too(tmp_path: Path) -> None:
    """It has no provider behind it either -- invoking a pattern would be a lie."""
    from src.server import _do_element

    session = quiet_session(tmp_path)
    session.start()
    icon = element("a paperclip", role="icon", rect=(10, 10, 40, 40), source="vision")
    with pytest.raises(ValueError, match="icon model"):
        _do_element(session, icon, "set_value", "hello")
    with pytest.raises(ValueError, match="icon model"):
        _do_element(session, icon, "invoke", "")
    session.stop()


# -- readiness --------------------------------------------------------------------------

def test_window_furniture_is_recognised_whatever_it_is_called() -> None:
    """Caption buttons carry the application's name, so exact matching fails.

    Getting this wrong made the Settings app look ready when all it had was a
    title bar, and its seventy-nine real elements were never read.
    """
    from src.control.session import _has_content, _is_furniture

    caption = [
        element("最小化 设置", role="button"),
        element("使 设置 最大化", role="button"),
        element("关闭 设置", role="button"),
        element("设置", role="window"),
    ]
    for item in caption:
        assert _is_furniture(item), f"{item.name!r} should count as window furniture"
    assert not _has_content(caption)

    from src.control.uia import Element

    real = Element(role="button", name="复制", rect=(10, 90, 60, 110), control_type=50000,
                   actions=("invoke",))
    assert not _is_furniture(real)
    assert _has_content(caption + [real])


def test_title_bar_class_counts_as_furniture() -> None:
    from src.control.session import _is_furniture
    from src.control.uia import Element

    assert _is_furniture(Element(role="window", name="设置", rect=(0, 0, 10, 10),
                                 class_name="ApplicationFrameTitleBarWindow"))


def test_asking_for_one_element_by_name_searches_the_whole_window(tmp_path: Path,
                                                                  monkeypatch) -> None:
    """The cap must limit the answer, not the search.

    python.org's "Downloads" link is the hundredth-odd element in the tree, so
    reading 50 and then filtering found nothing at all -- and said "the window
    has 50 element(s)", which is not true either.
    """
    from src.control import session as session_mod
    from src.control.uia import Element

    page = [Element(role="hyperlink", name=f"link {i}", rect=(10, 200 + i, 90, 220 + i),
                    actions=("invoke",)) for i in range(138)]
    page[97] = Element(role="hyperlink", name="Downloads", rect=(10, 300, 90, 320),
                       actions=("invoke",))
    seen: list[int] = []

    def fake_elements(hwnd, filter_mode, limit):
        seen.append(limit)
        return page[:limit], len(page) > limit

    session = quiet_session(tmp_path)
    session.start()
    try:
        monkeypatch.setattr(session.ui, "elements", fake_elements)
        monkeypatch.setattr(session_mod, "_window_region",
                            lambda hwnd: {"x": 0, "y": 0, "width": 1920, "height": 1048})
        found = session.observe("window:1", "interactive", 50, "Downloads")
        assert [e["name"] for e in found.to_dict()["elements"]] == ["Downloads"]
        assert max(seen) >= session_mod.QUERY_SCAN_LIMIT, "it should have looked past the cap"

        # Without a query the cap still applies to the reading, as before.
        # (It may read twice -- the tree looks half the size of the last one,
        # which earns a confirming look -- but never wider than it was asked.)
        seen.clear()
        plain = session.observe("window:1", "interactive", 50, "")
        assert len(plain.elements) == 50 and set(seen) == {50}

        # More matches than the cap: answer the first few and say it was cut.
        many = session.observe("window:1", "interactive", 10, "link")
        assert len(many.elements) == 10 and many.truncated is True
    finally:
        session.stop()


def test_typing_a_newline_strikes_return(monkeypatch) -> None:
    """U+000A through KEYEVENTF_UNICODE is not a Return, and nothing acts on it.

    Typing a URL that ended in a newline into the address bar filled it in and
    left it there: the browser only navigates on a real Return.
    """
    from src.control import inputs as inputs_mod

    struck: list[str] = []
    typed: list[str] = []
    monkeypatch.setattr(inputs_mod, "press", lambda keys, **kw: struck.append(keys))
    monkeypatch.setattr(inputs_mod, "_type_units",
                        lambda text, chunk, interval: typed.append(text))

    url = "https://example.com" + chr(10)
    assert inputs_mod.type_text(url) == len(url)
    assert typed == ["https://example.com"] and struck == ["enter"]

    struck.clear(); typed.clear()
    inputs_mod.type_text("first" + chr(13) + chr(10) + "second")
    assert typed == ["first", "second"] and struck == ["enter"]

    struck.clear(); typed.clear()
    inputs_mod.type_text("no newline here")
    assert typed == ["no newline here"] and struck == []


def test_a_loading_browser_is_not_mistaken_for_a_loaded_one(monkeypatch) -> None:
    """Edge answers with 40 elements of its own chrome for up to two seconds.

    Measured here by opening a window on two pages: while loading, exactly one
    element sits below the frame; loaded, python.org gives 86 and Wikipedia
    149. Counting cannot tell those apart, because the count is flat at 40
    while it waits -- the difference is where the elements are.
    """
    from src.control import session as session_mod
    from src.control.uia import Element

    monkeypatch.setattr(session_mod, "_window_region",
                        lambda hwnd: {"x": 0, "y": 0, "width": 1920, "height": 1048})

    def strip(count: int) -> list[Element]:
        """Elements in the top 160px: tabs, toolbar, caption buttons."""
        return [Element(role="button", name=f"tab {i}", rect=(40 * i, 8, 40 * i + 30, 30),
                        actions=("invoke",)) for i in range(count)]

    chrome = strip(40)
    assert session_mod._has_body_content(1, chrome) is False
    # The new-tab button sits just under the line, and one element is not a page.
    just_below = chrome + [Element(role="button", name="新建标签页", rect=(700, 161, 730, 185),
                                   actions=("invoke",))]
    assert session_mod._has_body_content(1, just_below) is False
    page = chrome + [Element(role="link", name=f"link {i}", rect=(100, 200 + i * 20, 300,
                                                                 215 + i * 20),
                             actions=("invoke",)) for i in range(86)]
    assert session_mod._has_body_content(1, page) is True
    # A minimised window cannot be waited for, so it must not look unready.
    monkeypatch.setattr(session_mod, "_window_region", lambda hwnd: None)
    assert session_mod._has_body_content(1, chrome) is True


def test_a_self_drawn_window_counts_as_having_no_content() -> None:
    """A Tk window's whole tree: the window, the system menu, three buttons.

    Every one of those is the frame. Counting the system menu as content meant
    the pixels were never read, so a window whose controls are all pictures
    came back with nothing usable in it.
    """
    from src.control.session import _has_content
    from src.control.uia import Element

    frame = [
        Element(role="window", name="codex-vision-probe", rect=(80, 140, 2016, 227)),
        Element(role="menuitem", name="系统", rect=(88, 148, 110, 170),
                actions=("invoke", "expand")),
        Element(role="button", name="最小化", rect=(1900, 148, 1930, 170), actions=("invoke",)),
        Element(role="button", name="最大化", rect=(1930, 148, 1960, 170), actions=("invoke",)),
        Element(role="button", name="关闭", rect=(1960, 148, 1990, 170), actions=("invoke",)),
    ]
    assert _has_content(frame) is False
    real = frame + [Element(role="button", name="发送", rect=(100, 200, 160, 230),
                            actions=("invoke",))]
    assert _has_content(real) is True
    # An application's own settings button is not the window frame.
    own = frame + [Element(role="button", name="系统设置", rect=(100, 200, 160, 230),
                           actions=("invoke",))]
    assert _has_content(own) is True


# -- the icon model ---------------------------------------------------------------------

def test_the_icon_model_reports_what_is_missing(tmp_path: Path) -> None:
    """Constructing it must not import torch or touch the card."""
    from src.control.vision import IconFinder, VisionUnavailable

    finder = IconFinder(tmp_path / "models")
    status = finder.status()
    assert status["loaded"] is False
    assert status["weights_present"] is False
    assert status["missing_weights"], "it should name the files it wants"
    with pytest.raises(VisionUnavailable, match="not downloaded yet|not installed"):
        finder.load()
    assert finder.unload()["was_loaded"] is False


def test_a_toolbar_strip_is_detected_at_its_own_size() -> None:
    """The taskbar is 1920x48; shrunk to 640 wide its icons are 13px tall."""
    from src.control.vision import IMAGE_SIZE, _input_size

    assert _input_size((1320, 818)) == IMAGE_SIZE      # an ordinary window
    assert _input_size((500, 400)) == IMAGE_SIZE       # smaller than the input
    assert _input_size((1920, 48)) == (64, 1920)       # the taskbar
    assert _input_size((60, 1200)) == (1216, 64)       # a vertical strip
    # Whatever comes back, the network needs both sides on its stride.
    for size in ((1920, 48), (60, 1200), (3840, 100)):
        assert all(v % 32 == 0 for v in _input_size(size))


def test_a_panel_sized_box_is_dropped_but_a_tall_icon_is_kept() -> None:
    """The filter has to be area, not edges, or the taskbar reads as empty."""
    from src.control.vision import MAX_ICON_AREA_FRACTION as LIMIT

    def kept(box: tuple[int, int, int, int], size: tuple[int, int]) -> bool:
        x0, y0, x1, y1 = box
        return (x1 - x0) * (y1 - y0) <= size[0] * size[1] * LIMIT

    assert kept((500, 1, 546, 47), (1920, 48)), "a 46px icon on a 48px strip"
    assert not kept((0, 0, 1900, 46), (1920, 48)), "the whole strip is not an icon"
    assert kept((40, 60, 80, 100), (1320, 818))
    assert not kept((0, 0, 700, 500), (1320, 818)), "a panel, not an icon"


def test_a_caption_model_saying_nothing_is_not_a_name() -> None:
    """Florence-2 answers "unanswerable" for pictures it cannot describe."""
    from src.control.vision import _tidy

    assert _tidy("<CAPTION>A settings gear.") == "A settings gear"
    assert _tidy("unanswerable") == ""
    assert _tidy("  Unanswerable. ") == ""
    assert _tidy("an image") == "", "too generic to tell two controls apart"
    assert _tidy("a" * 200) == "a" * 60, "names stay readable"


def test_an_icon_takes_the_name_printed_under_it() -> None:
    """The interface's own label beats a caption model's guess.

    Measured against the taskbar, where UI Automation knows the truth: the
    caption model was right about a quarter of the time and confidently wrong
    for the rest -- Edge's icon came back as "the mozilla firefox logo". A
    label printed under the button is not a guess at all.
    """
    from src.control import ocr

    icon = element("A simple icon or logo", role="icon", rect=(100, 100, 140, 140),
                   source="vision")
    label = element("消息", role="ocr_text", rect=(98, 144, 142, 160), source="ocr")
    far = element("通讯录", role="ocr_text", rect=(98, 400, 142, 416), source="ocr")
    beside = element("发送", role="ocr_text", rect=(145, 110, 190, 130), source="ocr")
    sentence = element("请在下方输入你要发送的内容", role="ocr_text",
                       rect=(98, 144, 300, 160), source="ocr")

    assert ocr.label_icons([icon], [label, far])[0].name == "消息"
    assert ocr.label_icons([icon], [beside])[0].name == "发送", "a label beside it counts too"
    assert ocr.label_icons([icon], [far])[0].name == "A simple icon or logo", "too far away"
    assert ocr.label_icons([icon], [sentence])[0].name == "A simple icon or logo",         "prose that happens to be nearby is not a label"
    # The nearest one wins when several qualify.
    nearer = element("日程", role="ocr_text", rect=(98, 142, 142, 158), source="ocr")
    assert ocr.label_icons([icon], [label, nearer])[0].name == "日程"


def test_icons_are_fused_like_any_other_guess() -> None:
    """A detected icon sitting on a named control is the same target."""
    from src.control import ocr

    button = element("发送", role="button", rect=(100, 100, 160, 130))
    on_top = element("send arrow", role="icon", rect=(110, 105, 150, 125), source="vision")
    elsewhere = element("gear", role="icon", rect=(400, 400, 430, 430), source="vision")
    fused = ocr.fuse([button], [on_top, elsewhere])
    assert [e.name for e in fused] == ["发送", "gear"]


@pytest.mark.asyncio
async def test_the_vision_tool_explains_itself_without_the_model(tmp_path: Path) -> None:
    from src.settings import ControlConf

    server: DeviceServer = device_server(
        tmp_path, control=ControlConf(vision_model_dir=str(tmp_path / "nowhere")))
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    status = (await tools["vision"].run({})).structured_content
    assert status["loaded"] is False and status["status"] == "ok"

    loaded = (await tools["vision"].run({"action": "load"})).structured_content
    assert loaded["status"] == "error"
    assert "fetch" in loaded["error"] or "install" in loaded["error"]

    bad = (await tools["vision"].run({"action": "sing"})).structured_content
    assert bad["status"] == "error" and "action must be" in bad["error"]


# -- the tool surface -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_control_tools_are_registered_and_annotated(tmp_path: Path) -> None:
    server: DeviceServer = device_server(tmp_path)
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    for name in ("start_control", "stop_control", "control_status",
                 "list_screens", "screenshot", "mouse", "keyboard"):
        assert name in tools
    assert tools["screenshot"].annotations.read_only_hint is True
    assert tools["list_screens"].annotations.read_only_hint is True
    # read_ui may raise a window to read it completely, so it does not claim
    # to be read-only even though reading is its whole purpose.
    assert tools["read_ui"].annotations.read_only_hint is False
    assert tools["read_ui"].annotations.destructive_hint is False
    # Taking control changes the screen and the pointer, so it is not read-only.
    assert tools["start_control"].annotations.read_only_hint is False
    assert tools["mouse"].annotations.read_only_hint is False


@pytest.mark.asyncio
async def test_control_tools_report_the_gate_instead_of_failing(tmp_path: Path) -> None:
    server: DeviceServer = device_server(tmp_path)
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    result = await tools["mouse"].run({"action": "move", "x": 10, "y": 10})
    assert "start_control" in str(result.structured_content or result.content)

    status = await tools["control_status"].run({})
    assert status.structured_content["active"] is False


# -- live behaviour, opt-in -------------------------------------------------------------

@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
def test_live_indicator_appears_on_every_monitor_and_leaves_no_trace(tmp_path: Path) -> None:
    from src.control.overlay import Overlay
    from src.control.screens import list_screens

    overlay = Overlay("ChatGPT 正在控制电脑", pulse=False)
    overlay.start()
    try:
        assert len(overlay._windows) == len(list_screens())
        assert overlay.alive
    finally:
        overlay.stop()
    assert overlay._windows == [] and not overlay.alive


@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
def test_live_injected_escape_does_not_end_control() -> None:
    """The model must be able to send ESC to an application.

    Only a key the person actually pressed ends the session; ``SendInput``
    marks its events as injected, and the hook ignores those.
    """
    from src.control.overlay import Overlay

    fired: list[str] = []
    overlay = Overlay("ChatGPT 正在控制电脑", pulse=False, on_escape=lambda: fired.append("esc"))
    overlay.start()
    try:
        assert overlay.escape_armed, "the escape hook should be installed"
        inputs.press("escape")
        time.sleep(0.5)
        assert fired == [], "an injected ESC must pass through to the application"
    finally:
        overlay.stop()
    assert not overlay.escape_armed


@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
def test_live_system_cursor_is_swapped_and_put_back(tmp_path: Path) -> None:
    marker = tmp_path / "cursors-replaced"
    swap = cursor_mod.SystemCursor(marker)
    assert swap.apply() is True
    try:
        assert marker.exists(), "the marker must exist while the cursors are swapped"
        assert swap.active
    finally:
        assert swap.restore() is True
    assert not marker.exists() and not swap.active


@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
def test_live_screenshot_of_a_small_region_decodes_as_an_image() -> None:
    from io import BytesIO

    from PIL import Image

    shot = capture_mod.capture(region={"x": 0, "y": 0, "width": 240, "height": 160}, max_edge=120)
    assert shot.mime in ("image/png", "image/jpeg") and shot.data
    assert (shot.x, shot.y, shot.width, shot.height) == (0, 0, 240, 160)
    assert shot.image_width == 120 and shot.scale == 0.5
    assert Image.open(BytesIO(shot.data)).size == (120, 80)


@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
def test_live_reads_a_dialog_by_name_and_presses_its_button(tmp_path: Path) -> None:
    """The whole point of the element layer, on a window we own ourselves.

    ``winver`` is a real dialog with real controls and nothing to lose: it is
    opened here, read by name, and closed through its own button.
    """
    import ctypes
    import subprocess
    from ctypes import wintypes

    from src.control.win32 import user32

    session = quiet_session(tmp_path)
    session.start()
    proc = subprocess.Popen(["winver.exe"])
    hwnd = 0
    try:
        for _ in range(40):
            time.sleep(0.2)
            found: list[int] = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def visit(handle, _param):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                if pid.value == proc.pid and user32.IsWindowVisible(handle):
                    found.append(int(handle))
                return True

            user32.EnumWindows(visit, 0)
            if found:
                hwnd = found[0]
                break
        assert hwnd, "winver did not open"

        observation = session.observe(f"window:{hwnd}")
        named = {element.name: ref for ref, element in observation.elements}
        assert "确定" in named, f"expected an OK button, got {sorted(named)}"

        ref = named["确定"]
        target = session.element(ref)
        assert "invoke" in target.actions
        session.ui.act(target, "invoke")
        time.sleep(0.8)
        assert not user32.IsWindow(hwnd), "invoking OK should have closed the dialog"

        # The element is gone: acting again must fail loudly, not click blindly,
        # and it must be a UiaError so callers can catch it.
        from src.control.uia import UiaError

        with pytest.raises(UiaError, match="gone"):
            session.ui.act(target, "invoke")
    finally:
        if hwnd and user32.IsWindow(hwnd):
            proc.terminate()
        session.stop()


@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
def test_live_a_covered_window_reports_its_own_pixels() -> None:
    """Regression: reading a window must not read whatever covers it.

    Grabbing the screen where a window sits returns the pixels of the window in
    front. Text recognised from those pixels was then reported as this window's
    controls -- observed live, with Paint's toolbar presented as the
    Calculator's buttons.
    """
    import subprocess

    from src.control import capture, ocr
    from src.control.uia import windows
    from src.control.win32 import user32

    def find(needle: str, timeout: float = 25.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for window in windows():
                if needle in window.name:
                    return window
            time.sleep(0.4)
        pytest.skip(f"no window named {needle!r} on this machine")

    subprocess.run(["taskkill", "/IM", "CalculatorApp.exe", "/F"], capture_output=True)
    subprocess.run(["taskkill", "/IM", "mspaint.exe", "/F"], capture_output=True)
    time.sleep(1)
    subprocess.Popen(["calc.exe"])
    calculator = find("计算器")
    paint = subprocess.Popen(["mspaint.exe"])
    covering = find("画图")
    try:
        left, top, right, bottom = calculator.rect
        user32.SetWindowPos(covering.hwnd, None, left - 20, top - 20,
                            right - left + 40, bottom - top + 40, 0x0040)
        user32.BringWindowToTop(covering.hwnd)
        user32.SetForegroundWindow(covering.hwnd)
        time.sleep(1.5)
        region = {"x": left, "y": top, "width": right - left, "height": bottom - top}

        own, _ = ocr.read_text(region, window_hwnd=calculator.hwnd)
        screen, _ = ocr.read_text(region, window_hwnd=0)
        own_text = " ".join(element.name for element in own)
        screen_text = " ".join(element.name for element in screen)

        assert "计算器" in own_text, f"the covered window should describe itself, got {own_text[:80]}"
        assert "画笔" in screen_text or "形状" in screen_text, (
            "the screen grab should show the covering window, which is the whole point"
        )
        assert "计算器" not in screen_text
        image, fell_back = capture.window_image(calculator.hwnd)
        assert not fell_back and image.size[0] > 100
    finally:
        subprocess.run(["taskkill", "/IM", "CalculatorApp.exe", "/F"], capture_output=True)
        subprocess.run(["taskkill", "/PID", str(paint.pid), "/F"], capture_output=True)


@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
def test_live_the_taskbar_and_desktop_are_reachable_by_name() -> None:
    """Both have no usable title, and both are full of named controls.

    The taskbar is a tool window and the desktop calls itself "Program
    Manager", so the ordinary filters dropped them -- and with them every
    pinned application and every desktop icon.
    """
    from src.control.uia import UiaReader, windows

    listed = {window.name: window for window in windows()}
    assert "任务栏" in listed or "任务栏（副屏）" in listed, sorted(listed)[:10]
    assert "桌面" in listed, sorted(listed)[:10]

    reader = UiaReader()
    taskbar = listed.get("任务栏") or listed["任务栏（副屏）"]
    buttons = {element.name for element in reader.elements(taskbar.hwnd, "interactive", 300)[0]}
    assert any("开始" in name for name in buttons), sorted(buttons)[:10]

    desktop = listed["桌面"]
    icons = reader.elements(desktop.hwnd, "interactive", 300)[0]
    assert any(icon.role == "listitem" and "invoke" in icon.actions for icon in icons)


@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
def test_live_window_list_is_fast_and_named(tmp_path: Path) -> None:
    from src.control.uia import windows

    started = time.time()
    found = windows()
    elapsed = time.time() - started
    assert found, "there should be at least one window open"
    assert all(window.name and window.role == "window" for window in found)
    # Win32 enumeration rather than UIA: this is the reason it is milliseconds.
    assert elapsed < 0.5, f"listing windows took {elapsed:.2f}s"


@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
def test_live_pointer_moves_where_it_is_told() -> None:
    from src.control.screens import list_screens

    screen = list_screens()[0]
    target = (screen.x + screen.width // 2, screen.y + screen.height // 2)
    origin = inputs.position()
    try:
        for _attempt in range(3):
            landed = inputs.move(*target)
            if abs(landed[0] - target[0]) <= 2 and abs(landed[1] - target[1]) <= 2:
                break
            # This machine has somebody sitting at it. A pointer that keeps
            # moving after we put it down is theirs, and their hand beats ours
            # -- that is a person using their computer, not a broken tool.
            time.sleep(0.2)
            if inputs.position() != landed:
                pytest.skip("the pointer is being moved by hand while this runs")
        else:
            pytest.fail(f"the pointer went to {landed}, not to {target}")
    finally:
        inputs.move(*origin)


@pytest.mark.skipif(not LIVE, reason="set CONTROL_LIVE_TESTS=1 to drive the real desktop")
@pytest.mark.asyncio
async def test_live_an_icon_only_window_is_read_and_clicked(tmp_path: Path) -> None:
    """The whole chain, on a window with no tree, in a process we own.

    The probe window shows a picture of this machine's own taskbar, so the
    detector has real icons to find and no accessibility tree describes them.
    It records where it is clicked, which is what makes the answer checkable: a
    name from the caption model has to lead to a click inside that icon's own
    rectangle, not merely to a click somewhere.

    It runs as a separate process rather than a thread. Tk belongs to the
    thread that made it, and an interpreter still alive at shutdown aborts the
    process with "Tcl_AsyncDelete: async handler deleted by the wrong thread"
    -- which happens after the assertions pass and takes pytest's report of
    them with it.
    """
    import subprocess
    import sys

    from src.control import capture
    from src.control.uia import UiaReader, windows
    from src.control.vision import IconFinder

    finder = IconFinder(Path("runtime/models/omniparser"))
    if not finder.packages_installed()[0] or not finder.weights_present()[0]:
        pytest.skip("the vision extra and its weights are needed for this one")

    taskbar = next((w for w in windows() if w.name == "任务栏"), None)
    assert taskbar is not None, "this is Windows; there is a taskbar"
    strip, _how, _note = capture.window_pixels(taskbar.hwnd, allow_focus=False)
    picture = tmp_path / "strip.png"
    strip.save(picture)
    clicked = tmp_path / "clicks.txt"
    title = "codex-vision-probe"
    probe = subprocess.Popen([sys.executable, str(Path(__file__).with_name("probe_window.py")),
                              str(picture), str(clicked), title])
    try:
        window = None
        for _ in range(40):
            time.sleep(0.5)
            window = next((w for w in windows() if title in w.name), None)
            if window is not None:
                break
        assert window is not None, "the probe window never opened"
        tree, _truncated = UiaReader().elements(window.hwnd, "interactive", 200)
        assert not _has_content_of(tree), "a Tk window should expose only its frame"

        server = device_server(tmp_path, default_workdir=str(tmp_path), control=ControlConf(
            vision_model_dir=str(Path("runtime/models/omniparser").resolve())))
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}

        async def call(name: str, args: dict | None = None) -> dict:
            return (await tools[name].run(args or {})).structured_content

        assert (await call("start_control"))["active"] is True
        try:
            assert (await call("vision", {"action": "load"}))["loaded"] is True
            read = await call("read_ui", {"scope": f"window:{window.hwnd}", "limit": 200})
            icons = [e for e in read["elements"] if e.get("source") == "vision"]
            assert icons, "the icons in the picture should have been found"
            assert any(e["name"] and not e["name"].startswith("未命名")
                       for e in icons), "the caption model should have named some of them"
            # The note has to agree with the list, or it sends the reader
            # looking for icons that fusion already dropped.
            assert f"{len(icons)} icons" in read["note"]

            target = icons[len(icons) // 2]
            done = await call("element", {"action": "click", "ref": target["ref"]})
            assert done["status"] == "ok" and done["source"] == "vision"
            assert "after" in done, "the action must come back with the new state"
            time.sleep(1.0)
            landed = [tuple(int(value) for value in line.split())
                      for line in clicked.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
            assert landed, "the window should have been clicked"
            x, y = landed[0]
            box = target["rect"]
            assert box["x"] <= x <= box["x"] + box["width"], "clicked outside the icon"
            assert box["y"] <= y <= box["y"] + box["height"], "clicked outside the icon"
        finally:
            await call("vision", {"action": "unload"})
            assert (await call("stop_control"))["active"] is False
    finally:
        probe.terminate()
        probe.wait(timeout=10)
