"""Activation needs a dispatcher; construction-time model changes stay silent."""

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost

from pysual import (App, Button, CheckBox, DataGrid, GridColumn, GridRow,
                    Hyperlink, RadioButton, Toggle)
from pysual.errors import LifecycleError


class ActivationSetupTests(UIOwnerTestCase):
    def test_detached_activation_rejects_before_state_changes_with_or_without_listeners(self):
        for control_type in (Button, CheckBox, Toggle, RadioButton, Hyperlink):
            for subscribed in (False, True):
                with self.subTest(control=control_type.__name__, subscribed=subscribed):
                    app = App()
                    control = control_type(parent=app)
                    peer = RadioButton(parent=app, checked=True)
                    if isinstance(control, Hyperlink):
                        control.url = "https://example.com/"
                    seen = []
                    if subscribed:
                        control.click.connect(seen.append)
                    try:
                        with self.assertRaisesRegex(LifecycleError, "Activation requires a running App"):
                            control.activate()
                        self.assertFalse(getattr(control, "checked", False))
                        self.assertTrue(peer.checked)
                        self.assertEqual(seen, [])
                    finally:
                        app.destroy()

    def test_setup_data_changes_remain_silent_with_connected_listeners(self):
        app = App()
        control = CheckBox(parent=app)
        grid = DataGrid(parent=app, columns=(GridColumn("v", "Value", kind="number"),),
                        rows=(GridRow("row", (1,)),))
        seen = []
        control.changed.connect(seen.append)
        grid.edited.connect(seen.append)
        try:
            control.checked = True
            grid.set_cell("row", "v", 2)
            self.assertTrue(control.checked)
            self.assertEqual(grid.rows[0].cells, (2,))
            self.assertEqual(seen, [])
        finally:
            app.destroy()

    def test_activation_in_build_fails_before_opening_the_host(self):
        class Demo(App):
            def build(self):
                self.action = Toggle()
                self.action.activate()

        app, host = Demo(), RecordingHost()
        try:
            with self.assertRaisesRegex(LifecycleError, "loaded handler"):
                app.run(backend=host)
            self.assertFalse(host.opened)
        finally:
            app.destroy()


class LiveActivationTests(AsyncUIOwnerTestCase):
    async def test_loaded_activation_dispatches_and_closed_activation_rejects(self):
        app = App()
        app.action = Toggle()
        seen = []
        app.action.click.connect(seen.append)
        app.loaded.connect(lambda event: app.action.activate())
        try:
            app.run(backend=RecordingHost())
            await app._runtime.dispatcher.drain()
            self.assertTrue(app.action.checked)
            self.assertEqual(len(seen), 1)
            app.close()
            await app.wait_async()
            with self.assertRaisesRegex(LifecycleError, "Activation requires"):
                app.action.activate()
            self.assertTrue(app.action.checked)
            app.run(backend=RecordingHost())
            await app._runtime.dispatcher.drain()
            self.assertFalse(app.action.checked)
            self.assertEqual(len(seen), 2)
        finally:
            await app.destroy_async()
