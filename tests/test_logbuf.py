"""claudeide.logbuf: background threads buffer, the main thread prints."""

import threading

from claudeide import logbuf


def _reset():
    logbuf.remember_main_thread()
    logbuf.set_enabled(True)
    logbuf.drain()


def test_disabled_logs_nothing(capsys):
    _reset()
    logbuf.set_enabled(False)
    logbuf.log("hidden")
    assert logbuf.pending() == 0
    assert capsys.readouterr().out == ""


def test_main_thread_prints_immediately(capsys):
    _reset()
    logbuf.log("now")
    assert capsys.readouterr().out.strip() == "now"
    assert logbuf.pending() == 0


def test_background_thread_buffers_until_drain(capsys):
    _reset()
    t = threading.Thread(target=lambda: logbuf.log("later"))
    t.start()
    t.join()
    assert capsys.readouterr().out == ""      # nothing printed off-main
    assert logbuf.pending() == 1
    assert logbuf.drain() == 1
    assert capsys.readouterr().out.strip() == "later"
    assert logbuf.pending() == 0


def test_main_thread_log_flushes_buffer_first(capsys):
    _reset()
    t = threading.Thread(target=lambda: logbuf.log("bg"))
    t.start()
    t.join()
    logbuf.log("main")
    assert capsys.readouterr().out.split() == ["bg", "main"]


def test_buffer_is_bounded():
    _reset()

    def spam():
        for i in range(logbuf._MAX_BUFFERED + 50):
            logbuf.log(str(i))

    t = threading.Thread(target=spam)
    t.start()
    t.join()
    assert logbuf.pending() == logbuf._MAX_BUFFERED
    logbuf.drain()
