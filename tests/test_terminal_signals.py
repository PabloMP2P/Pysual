"""SIGTERM/SIGHUP restore open Python terminal sessions from the main thread."""

import signal
import threading
import unittest

from pysual import App, Label, terminal
from pysual.backends import _term_session
from pysual.backends.term_text import TermTextHost
from pysual.backends.terminal import prepare_signal_restoration
from _terminal import MemoryTerminal


class Demo(App):
    def build(self):
        self.caption = Label(text="x")


class TerminalSignalTests(unittest.TestCase):
    def setUp(self):
        self.assertIs(threading.current_thread(), threading.main_thread())
        _term_session._reset_signal_restoration()
        self.addCleanup(_term_session._reset_signal_restoration)

    def finish(self, app):
        app.destroy()
        app.wait(timeout=5)

    def test_running_a_python_terminal_from_the_main_thread_owns_restoration(self):
        app = Demo()
        app.run(backend=terminal(renderer="python", hidden=True))
        try:
            self.assertIs(
                signal.getsignal(signal.SIGTERM), _term_session._restore_terminals
            )
            self.assertIn(app._runtime.host._host, _term_session._open_sessions)
        finally:
            self.finish(app)
        self.assertFalse(
            any(session._active for session in _term_session._open_sessions)
        )

    def test_other_backends_and_the_c_renderer_install_nothing(self):
        self.assertFalse(prepare_signal_restoration("window"))
        self.assertFalse(prepare_signal_restoration(terminal(renderer="c", hidden=True)))
        self.assertFalse(_term_session._restoration_signals)
        self.assertIsNot(
            signal.getsignal(signal.SIGTERM), _term_session._restore_terminals
        )

    def test_installation_is_main_thread_only_and_idempotent(self):
        results = []
        worker = threading.Thread(
            target=lambda: results.append(_term_session.install_signal_restoration())
        )
        worker.start()
        worker.join()
        self.assertEqual(results, [False])
        self.assertTrue(_term_session.install_signal_restoration())
        installed = dict(_term_session._restoration_signals)
        self.assertTrue(_term_session.install_signal_restoration())
        self.assertEqual(_term_session._restoration_signals, installed)

    def test_the_handler_restores_the_console_and_then_terminates(self):
        io = MemoryTerminal()
        host = TermTextHost(io=io, probe_timeout=0)
        app = Demo()
        app.run(backend=host)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            self.assertIs(handler, _term_session._restore_terminals)
            previous = _term_session._restoration_signals[signal.SIGTERM]
            if previous == signal.SIG_DFL:
                with self.assertRaises(SystemExit) as caught:
                    handler(signal.SIGTERM, None)
                self.assertEqual(caught.exception.code, 128 + int(signal.SIGTERM))
            else:
                handler(signal.SIGTERM, None)
            self.assertEqual(io.exited, 1)
            self.assertTrue(any(b"\x1b[?1049l" in chunk for chunk in io.output))
            self.assertFalse(host._active)
        finally:
            self.finish(app)
        # Closing an already restored session must not write a second leave.
        self.assertEqual(io.exited, 1)


if __name__ == "__main__":
    unittest.main()
