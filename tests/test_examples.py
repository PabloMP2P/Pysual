"""Examples exercise the public runtime and real portable drawing adapters."""

import importlib
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from pysual import ClickEvent, get_theme, terminal, theme_names
from pysual._engine import call
from pysual.backends._web_native import LiveSVGHost
from pysual.host import Input
from test_library import RecordingHost


@pytest.mark.parametrize('name', ['hello', 'custom_control', 'catalog_controls', 'theme_gallery'])
@pytest.mark.parametrize('flag,code', [('--help', 0), ('--unknown-option', 2)])
def test_small_examples_parse_arguments_before_opening(name, flag, code):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / 'examples' / f'{name}.py'), flag],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, 'PYTHONPATH': str(root / 'src')},
    )
    assert result.returncode == code
    assert 'usage:' in result.stdout + result.stderr


def test_dashboard_displays_actual_filtered_revenue():
    from examples.dashboard import Dashboard

    app = Dashboard(reduce_motion=True)
    def total(expected):
        deadline = time.monotonic() + 5
        while app.chart.center_text != expected and time.monotonic() < deadline:
            time.sleep(.005)
        assert app.chart.center_text == expected
    try:
        app.run(backend=RecordingHost())
        total('5740')
        assert app.chart.center_mode == 'percent'
        app.query.text = 'North'
        total('1830')
        app.grid.set_cell('0', 'revenue', 2400, origin='user')
        total('3030')
        app.query.text = 'no matching account'
        total('0')
        app.query.text = ''
        total('6940')
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()


@pytest.mark.parametrize('module,name', [
    ('hello', 'Hello'), ('dashboard', 'Dashboard'),
    ('showcase', 'Showcase'), ('custom_control', 'Demo'),
    ('catalog_controls', 'Demo'), ('theme_gallery', 'ThemeGallery'),
])
def test_examples_open_paint_and_close(module, name):
    cls = getattr(importlib.import_module(f'examples.{module}'), name)
    app, host = cls(reduce_motion=True), RecordingHost()
    try:
        app.run(backend=host)
        assert app.is_open and host.frames >= 1
        assert host.texts
        app.close()
        app.wait(timeout=5)
        assert host.closed
    finally:
        app.destroy()


@pytest.mark.parametrize('renderer', ['svg', 'python-terminal'])
def test_catalog_controls_compose_through_portable_renderers(renderer):
    from examples.catalog_controls import Demo
    host = (LiveSVGHost(open_browser=False) if renderer == 'svg'
            else terminal(renderer='python', hidden=True))
    app = Demo(reduce_motion=True)

    def wait_until(predicate):
        deadline = time.monotonic() + 5
        while not call(predicate) and time.monotonic() < deadline:
            time.sleep(.005)
        assert call(predicate)

    def visible_reviews():
        return [row.subject.text for row in app._rows if row.visible]

    try:
        app.run(backend=host)
        assert app.pages.page_count == 3
        app.pages.page = 2
        wait_until(lambda: 'Theme contrast' in visible_reviews())
        app.review_view.selected_index = 2
        wait_until(lambda: app.completion.value == 100 and app.pages.page == 1)
        app.effort.value = (0, 4)
        wait_until(lambda: visible_reviews() == ['Keyboard navigation'])
        app.effort.value = (0, 12)
        app.review_view.selected_index = 0
        app.query.text = 'keyboard'
        wait_until(lambda: visible_reviews() == ['Keyboard navigation'])
        wait_until(lambda: app.pages.page_count == 1 and app.completion.value == 100)
        app.query.text = 'no matching review'
        wait_until(lambda: not app.pages.enabled and app.completion.value == 0)
        assert app.empty.visible
        app.path.activate(0)
        wait_until(lambda: app.query.text == '' and app.pages.page_count == 3)
        for name in ('macos', 'windows_dark', 'win31', 'analog_arcade_dark'):
            app.theme_choice.selected_index = theme_names().index(name)
            wait_until(lambda: app.theme == get_theme(name))
        app.score.value = 5
        wait_until(lambda: '5/5' in app.status.text and app.readiness.value == 100)
        app.full_review.checked = True
        wait_until(lambda: not app.focused_review.checked and 'Full pass' in app.status.text)
        app.notice.activate()
        wait_until(lambda: app.review_view.selected_index == 2 and app.completion.value == 100)
        app.notice.dismiss()
        assert not app.notice.visible
        app.save_plan.activate()
        wait_until(lambda: app.notice.visible and app.notice.tone == 'success')
        app.calendar_details.expanded = True
        assert app.calendar.read() == '2026-10-20'
        call(lambda: app.use_date.click.emit(ClickEvent(source=app.use_date, origin='user')))
        wait_until(lambda: app.due.value == '2026-10-20')
        assert not app.calendar_details.expanded
        app.due.open()
        wait_until(lambda: app._runtime.popup is not None)
        app.due.destroy()
        wait_until(lambda: app._runtime.popup is None)
        assert app.calendar.read() == '2026-10-20'
        app.close()
        app.wait(timeout=5)
    finally:
        app.destroy()


@pytest.mark.parametrize('renderer', ['svg', 'python-terminal'])
def test_hello_async_binding_through_portable_renderer(renderer):
    from examples.hello import Hello
    host = (LiveSVGHost(open_browser=False) if renderer == 'svg'
            else terminal(renderer='python', hidden=True))
    app = Hello(reduce_motion=True)
    try:
        app.run(backend=host)
        app.name_entry.text = 'Ada'
        call(lambda: app.greet.click.emit(ClickEvent(source=app.greet, origin='user')))
        deadline = time.monotonic() + 5
        while app.message.text != 'Hello, Ada!' and time.monotonic() < deadline:
            time.sleep(0.01)
        assert app.message.text == 'Hello, Ada!'
        app.close()
        app.wait(timeout=5)
    finally:
        app.destroy()


def test_catalog_workspace_resize_preserves_plan_and_readable_choices():
    from examples.catalog_controls import Demo

    app = Demo(reduce_motion=True)
    try:
        app.run(backend=RecordingHost())
        app.query.text = 'keyboard'
        app.full_review.checked = True
        for width, columns, stacked in ((375, 1, True), (700, 1, False),
                                         (900, 2, True), (1180, 2, False)):
            app.update(width=width, height=800)
            deadline = time.monotonic() + 5
            def arranged():
                return (app.bounds.width == width
                        and len(app.workspace.column_tracks) == columns
                        and app.full_review.bounds.width >= 180
                        and ((app.full_review.bounds.y > app.focused_review.bounds.y) == stacked))
            while not call(arranged) and time.monotonic() < deadline:
                time.sleep(.005)
            assert call(arranged)
            assert app.query.text == 'keyboard' and app.full_review.checked
            assert app.plan_panel.bounds.right <= app.bounds.right
        app.calendar_details.expanded = True
        deadline = time.monotonic() + 5
        while app.use_date.bounds.height == 0 and time.monotonic() < deadline:
            time.sleep(.005)
        assert app.use_date.bounds.height > 0
        assert app.body.scroll_y == 0
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()


@pytest.mark.parametrize('renderer', ['svg', 'python-terminal'])
def test_theme_gallery_opens_in_both_portable_renderers(renderer):
    from examples.theme_gallery import ThemeGallery

    host = (LiveSVGHost(open_browser=False) if renderer == 'svg'
            else terminal(renderer='python', hidden=True))
    app = ThemeGallery(reduce_motion=True, theme=get_theme('winxp'))
    try:
        app.run(backend=host)
        assert app.theme == get_theme('winxp')
        assert app.family_choice.selected_index == 5
        assert app.appearance.selected_index == 0
        assert len(app._specimens) == 6
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()


@pytest.mark.parametrize('name', ['modern', 'modern_dark'])
def test_gallery_secondary_actions_keep_neutral_faces(name):
    from examples.theme_gallery import SecondaryButton
    from pysual import Button, Dropdown
    from pysual.painting import _style_kinds

    theme = get_theme(name)
    secondary = theme.resolve(_style_kinds(SecondaryButton))
    primary = theme.resolve(_style_kinds(Button))
    dropdown = theme.resolve(_style_kinds(Dropdown))
    assert secondary.fill == dropdown.fill
    assert secondary.fill != primary.fill
    assert secondary.foreground == dropdown.foreground


def test_theme_gallery_switches_every_appearance_without_resetting_edits():
    from examples.theme_gallery import FAMILIES, ThemeGallery
    from pysual import CommandEvent

    app = ThemeGallery(reduce_motion=True)

    def wait_until(predicate):
        deadline = time.monotonic() + 5
        while not call(predicate) and time.monotonic() < deadline:
            time.sleep(.005)
        assert call(predicate)

    def command(key):
        call(lambda: app.command_bar.command.emit(CommandEvent(source=app.command_bar, key=key, origin='user')))

    try:
        app.run(backend=RecordingHost())
        app.project_name.text = 'Keep my workspace'
        app.intensity.value = 27
        app.copies.value = 7
        app.live.checked = False
        app.notifications.checked = True
        app.hidden_layers.checked = True
        app.compact.checked = True
        app.rating.value = 2
        app.snap.checked = False
        app.period.selected_index = 2
        app.destination.selected_index = 1
        app.date.value = '2026-11-03'
        app.files.selected_key = '3'
        app.disclosure.expanded = True
        wait_until(lambda: app.progress.value == app.meter.value == 27)
        for index, (family, *_) in enumerate(FAMILIES):
            app.family_choice.selected_index = index
            for night in (False, True):
                app.appearance.selected_index = int(night)
                expected = get_theme(family + ('_dark' if night else ''))
                wait_until(lambda: app.theme == expected)
                assert app.project_name.text == 'Keep my workspace'
                assert app.intensity.value == 27 and app.copies.value == 7
                assert not app.live.checked
                assert app.notifications.checked and app.hidden_layers.checked
                assert app.compact.checked and not app.standard.checked
                assert sum(link.active for link in app._theme_links) == 1
                assert app._theme_links[index].active
        command('save')
        wait_until(lambda: 'SAVED / Keep my workspace' in app.footer.text)
        command('new')
        wait_until(lambda: app.project_name.text == 'Untitled project')
        command('light')
        wait_until(lambda: app.theme == get_theme('neon'))
        command('dark')
        wait_until(lambda: app.theme == get_theme('neon_dark'))
        command('help')
        wait_until(lambda: 'KEYBOARD /' in app.footer.text)
        app.search.text = 'dates'
        wait_until(lambda: sum(section.visible for section in app._specimens) == 1)
        command('new')
        wait_until(lambda: app.search.text == '' and app.project_name.focused
                   and app.scroll.scroll_y == 0 and all(section.visible for section in app._specimens))
        app.search.text = 'selection'
        wait_until(lambda: sum(section.visible for section in app._specimens) == 1)
        assert not app.empty.visible
        app.search.text = 'no matching specimen'
        wait_until(lambda: app.empty.visible and not any(section.visible for section in app._specimens))
        command('reset')
        wait_until(lambda: all(section.visible for section in app._specimens) and not app.empty.visible)
        assert app.theme == get_theme('neon_dark')
        assert app.project_name.text == 'A little possibility'
        assert app.intensity.value == 68 and app.copies.value == 3 and app.live.checked
        assert not app.notifications.checked and not app.hidden_layers.checked
        assert app.standard.checked and not app.compact.checked
        assert app.rating.value == 4 and app.snap.checked
        assert app.period.selected_index == 1 and app.destination.selected_index == 0
        assert app.date.value == '2026-10-15' and app.files.selected_key == '1'
        assert not app.disclosure.expanded
        app.dialog_button.activate()
        wait_until(lambda: app.dialog.visible and app._runtime.modal is app.dialog)
        app.close_dialog.activate()
        wait_until(lambda: not app.dialog.visible and app._runtime.modal is None)
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()


def test_theme_gallery_short_desktop_keeps_motion_preference_reachable():
    from examples.theme_gallery import ThemeGallery

    app = ThemeGallery(reduce_motion=True)
    try:
        app.run(backend=RecordingHost())
        app.update(width=1100, height=640)
        app.reduce.focus()
        deadline = time.monotonic() + 5
        def reachable():
            return (app.reduce.focused and app.sidebar.scroll_y > 0
                    and app.reduce._clip.height == app.reduce.bounds.height)
        while not call(reachable) and time.monotonic() < deadline:
            time.sleep(.005)
        assert call(reachable)
        app.reduce.activate()
        deadline = time.monotonic() + 5
        while app.reduce_motion and time.monotonic() < deadline:
            time.sleep(.005)
        assert not app.reduce_motion
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()


def test_theme_gallery_layout_stays_usable_from_phone_to_desktop():
    from examples.theme_gallery import ThemeGallery

    app = ThemeGallery(reduce_motion=True)
    try:
        app.run(backend=RecordingHost())
        app.project_name.text = 'Preserved while resizing'
        app.dialog_button.activate()
        for width, columns, sidebar in ((375, 1, False), (700, 1, False),
                                         (1100, 2, True), (1440, 3, True)):
            app.update(width=width, height=800)
            deadline = time.monotonic() + 5
            def arranged():
                return (app.bounds.width == width
                        and len(app.grid.column_tracks) == columns
                        and app.sidebar.visible == sidebar
                        and all(section.bounds.width >= 280 for section in app._specimens)
                        and app.dialog.visible and app._runtime.modal is app.dialog
                        and app.dialog.bounds.x >= 8
                        and app.dialog.bounds.right <= width - 8)
            while not call(arranged) and time.monotonic() < deadline:
                time.sleep(.005)
            assert call(arranged)
            assert app.project_name.text == 'Preserved while resizing'
            assert app.family_choice.bounds.width >= 140
            assert app.appearance.bounds.right <= app.bounds.right
            assert app.grid.bounds.right <= app.scroll.bounds.right
            assert app.close_dialog.bounds.right <= app.dialog.bounds.right
        app.close_dialog.activate()
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()


@pytest.mark.parametrize('name', ['default', 'winxp', 'custom'])
def test_showcase_keeps_its_startup_theme(name):
    from examples.showcase import Showcase

    theme = get_theme('modern_dark' if name == 'default' else 'winxp')
    if name == 'custom':
        theme = theme.derive(surface='#193e31')
    app = Showcase(reduce_motion=True, **({} if name == 'default' else {'theme': theme}))
    try:
        app.run(backend=RecordingHost())
        assert app.theme == theme
        expected = -1 if name == 'custom' else theme_names().index(
            'modern_dark' if name == 'default' else name
        )
        assert app.theme_choice.selected_index == expected
        assert app.sidebar.background == app.workspace.background == theme.tokens.surface
        if name == 'custom':
            assert app.theme_choice.placeholder == 'Custom theme'
        # Selecting a catalog entry must still replace the current theme.
        app.theme_choice.selected_index = theme_names().index('modern')
        deadline = time.monotonic() + 5
        def theme_applied():
            return (app.theme == get_theme('modern')
                    and app.sidebar.background == app.workspace.background == app.theme.tokens.surface)
        # Observe the completed owner-thread handler, not its first assignment.
        while not call(theme_applied) and time.monotonic() < deadline:
            time.sleep(.01)
        assert call(theme_applied)
        app.close()
        app.wait(timeout=5)
    finally:
        app.destroy()


def test_showcase_small_window_settings_scroll_and_preserve_edits():
    from examples.showcase import Showcase

    app = Showcase(reduce_motion=True)

    def wait_until(predicate):
        deadline = time.monotonic() + 5
        while not call(predicate) and time.monotonic() < deadline:
            time.sleep(.005)
        assert call(predicate)

    def wheel_sidebar():
        bounds = app.sidebar.bounds
        app._runtime._process_input(Input(
            'wheel', x=bounds.x + 30, y=bounds.bottom - 20, delta=10,
        ))

    def click_cache():
        bounds = app.cache_enabled.bounds
        for kind in ('pointer_down', 'pointer_up'):
            app._runtime._process_input(Input(kind, x=bounds.x + 10, y=bounds.y + 10))

    try:
        app.run(backend=RecordingHost())
        app.animate.checked = False
        app.show_page('Theme lab')
        entry = app.lab_entry
        entry.text = 'Keep this edit while resizing'
        app.update(width=600, height=400)
        wait_until(lambda: app.bounds.width == 600 and not app.meter.visible)
        call(wheel_sidebar)
        wait_until(lambda: app.cache_enabled._clip.height == app.cache_enabled.bounds.height
                   and app.cache_memory._clip.height == app.cache_memory.bounds.height)
        assert app.sidebar.scroll_y > 0
        checked = app.cache_enabled.checked
        assert not app.cache_enabled.enabled  # RecordingHost has no surface cache.
        call(click_cache)
        assert app.cache_enabled.checked == checked
        app.theme_choice.selected_index = theme_names().index('modern')
        wait_until(lambda: app.theme == get_theme('modern'))
        app.update(width=960, height=640)
        wait_until(lambda: app.bounds.width == 960 and app.meter.visible
                   and app.sidebar.scroll_y == 0)
        assert app.lab_entry is entry
        assert entry.text == 'Keep this edit while resizing'
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()


@pytest.mark.parametrize('renderer', ['retained', 'surface', 'direct'])
def test_showcase_labels_active_renderer_and_uses_local_submission_counter(renderer, monkeypatch):
    from examples.showcase import Showcase

    def unexpected_frame_count(self):
        raise AssertionError('Showcase must not poll the native presentation counter')

    monkeypatch.setattr(Showcase, 'frame_count', property(unexpected_frame_count))
    host = (LiveSVGHost(open_browser=False) if renderer == 'retained' else
            terminal(renderer='python', hidden=True) if renderer == 'surface' else RecordingHost())
    app = Showcase(reduce_motion=True)
    try:
        app.run(backend=host)
        app.animate.checked = False
        call(app.update_cache_meter)
        assert app.cache_enabled.text == 'Surface cache'
        assert app.cache_enabled.enabled == (renderer == 'surface')
        if renderer == 'retained':
            assert app.cache_meter.text == 'Retained scenes'
            app.render_cache_bytes = 0
            call(app.update_cache_meter)
            assert app.cache_meter.text == 'Retained scenes'
            assert app.cache_memory.text == ''
        elif renderer == 'surface':
            assert 'stored' in app.cache_meter.text
            budget = app.render_cache_bytes
            app.cache_enabled.checked = False
            deadline = time.monotonic() + 5
            while app.render_cache_bytes and time.monotonic() < deadline:
                time.sleep(.005)
            assert app.render_cache_bytes == 0
            assert app.cache_meter.text == 'Direct painting'
            app.cache_enabled.checked = True
            deadline = time.monotonic() + 5
            while app.render_cache_bytes != budget and time.monotonic() < deadline:
                time.sleep(.005)
            assert app.render_cache_bytes == budget
        else:
            assert app.cache_meter.text == 'Host: direct fallback'
            assert app.cache_memory.text == ''
        deadline = time.monotonic() + 5
        while app.meter.text == 'Scene submissions/s —' and time.monotonic() < deadline:
            time.sleep(.01)
        assert app.meter.text.startswith('Scene submissions/s ')
        assert '—' not in app.meter.text
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()
