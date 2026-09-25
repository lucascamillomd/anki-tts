"""Stubbed aqt / aqt.qt / aqt.utils for tests (no real Anki needed).

Shared by the reviewer-hook and monitor tests. ``_install_fake_aqt()``
installs fresh stub modules into ``sys.modules`` and returns the ``hooks``
namespace, which also records menus, tooltips, timers and background tasks.
"""

import sys
import types


_AQT_MODULES = ("aqt", "aqt.qt", "aqt.utils")
_MISSING = object()
_DEFAULT_CONFIG = {
    "enabled": True,
    "speak_question": True,
    "speak_answer": True,
    "fallback_to_system": True,
    "speed": 1.5,
    "cache_enabled": True,
    "prefetch_enabled": True,
    "cache_max_mb": 2048,
}


class _FakeAddonManager:
    def __init__(self):
        self.config = dict(_DEFAULT_CONFIG)

    def getConfig(self, _name):
        return dict(self.config)

    def writeConfig(self, _name, conf):
        self.config = dict(conf)


class _ImmediateFuture:
    def __init__(self, task):
        self._result = None
        self._error = None
        try:
            self._result = task()
        except Exception as error:
            self._error = error

    def result(self):
        if self._error is not None:
            raise self._error
        return self._result


def _install_fake_aqt():
    hooks = types.SimpleNamespace(
        card_will_show=[],
        reviewer_did_show_question=[],
        reviewer_did_show_answer=[],
        reviewer_will_end=[],
        profile_did_open=[],
        profile_will_close=[],
        sync_will_start=[],
        sync_did_finish=[],
        top_toolbar_did_init_links=[],
        top_toolbar_did_redraw=[],
        menus=[],
        tooltips=[],
        timers=[],
        background_calls=[],
        ask_user_answers=[],
        ask_user_questions=[],
        opened_folders=[],
        docks=[],
    )

    def add_menu(menu):
        hooks.menus.append(menu)

    def run_in_background(task, on_done=None, *_args, **kwargs):
        hooks.background_calls.append(
            {"task": task, "on_done": on_done, "kwargs": dict(kwargs)}
        )
        # Background work runs when its "timer" is drained, like a callback
        # that lands on the main thread later.
        def _complete():
            future = _ImmediateFuture(task)
            if on_done is not None:
                on_done(future)

        hooks.timers.append((0, _complete))

    aqt_module = types.ModuleType("aqt")
    aqt_module.gui_hooks = hooks
    aqt_module.mw = types.SimpleNamespace(
        addonManager=_FakeAddonManager(),
        taskman=types.SimpleNamespace(
            run_on_main=lambda fn: fn(),
            run_in_background=run_in_background,
        ),
        col=None,
        form=types.SimpleNamespace(
            menubar=types.SimpleNamespace(addMenu=add_menu)
        ),
        toolbar=FakeToolbar(),
        addDockWidget=lambda area, dock: hooks.docks.append((area, dock)),
    )

    qt_module = types.ModuleType("aqt.qt")

    class _FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class _FakeAction:
        def __init__(self, *args, **_kwargs):
            self.text = args[0] if args else ""
            self.triggered = _FakeSignal()
            self.shortcut = None

        def setShortcut(self, shortcut):
            self.shortcut = shortcut

    class _FakeMenu:
        def __init__(self, *args, **_kwargs):
            self.title = args[0] if args else ""
            self.actions = []

        def addAction(self, action):
            self.actions.append(action)

    class _FakeTimer:
        instances = []

        def __init__(self, *_args, **_kwargs):
            self.timeout = _FakeSignal()
            self._interval = 0
            self._active = False
            self.start_count = 0
            _FakeTimer.instances.append(self)

        @staticmethod
        def singleShot(delay_ms, callback):
            hooks.timers.append((delay_ms, callback))

        def start(self, *_args):
            self._active = True
            self.start_count += 1

        def stop(self):
            self._active = False

        def setInterval(self, ms):
            self._interval = ms

        def interval(self):
            return self._interval

        def isActive(self):
            return self._active

    class _FakeWidget:
        def __init__(self, *_args, **_kwargs):
            pass

    qt_module.QAction = _FakeAction
    qt_module.QMenu = _FakeMenu
    qt_module.QDialog = _FakeWidget
    qt_module.QVBoxLayout = FakeLayout
    qt_module.QHBoxLayout = FakeLayout
    qt_module.QLabel = FakeLabel
    qt_module.QDoubleSpinBox = _FakeWidget
    qt_module.QCheckBox = _FakeWidget
    qt_module.QPushButton = FakePushButton
    qt_module.QGroupBox = _FakeWidget
    qt_module.QTimer = _FakeTimer
    qt_module.QDockWidget = FakeDockWidget
    qt_module.QWidget = FakeQtWidget
    qt_module.QGridLayout = FakeLayout
    qt_module.QProgressBar = FakeProgressBar
    qt_module.QListWidget = FakeListWidget
    qt_module.Qt = FakeQt
    hooks.timer_class = _FakeTimer

    utils_module = types.ModuleType("aqt.utils")
    utils_module.tooltip = lambda msg: hooks.tooltips.append(msg)

    def ask_user(question, *_args, **_kwargs):
        hooks.ask_user_questions.append(question)
        return hooks.ask_user_answers.pop(0) if hooks.ask_user_answers else True

    utils_module.askUser = ask_user
    utils_module.openFolder = lambda path: hooks.opened_folders.append(path)

    sys.modules["aqt"] = aqt_module
    sys.modules["aqt.qt"] = qt_module
    sys.modules["aqt.utils"] = utils_module

    return hooks




class FakeWeb:
    def __init__(self):
        self.evals = []

    def eval(self, js):
        self.evals.append(js)


class FakeToolbar:
    def __init__(self):
        self.web = FakeWeb()
        self.link_handlers = {}
        self.draw_count = 0

    def draw(self):
        self.draw_count += 1


class _Recorder:
    """Accepts any setter call and remembers the last value per setter."""

    def __init__(self, *args, **_kwargs):
        self.args = args
        self.values = {}
        self.children = []

    def __getattr__(self, name):
        if name.startswith("set"):
            def _setter(*args):
                self.values[name] = args[0] if len(args) == 1 else args
            return _setter
        if name.startswith("add"):
            def _adder(*args):
                self.children.append((name, args))
            return _adder
        raise AttributeError(name)


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class FakeQtWidget(_Recorder):
    pass


class FakeLayout(_Recorder):
    pass


class FakeLabel(_Recorder):
    def setText(self, text):
        self.values["setText"] = text

    def text(self):
        return self.values.get("setText", self.args[0] if self.args else "")


class FakePushButton(FakeLabel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clicked = FakeSignal()


class FakeProgressBar(_Recorder):
    def value(self):
        return self.values.get("setValue")

    def format(self):
        return self.values.get("setFormat")


class FakeListWidget(_Recorder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.items = []

    def clear(self):
        self.items = []

    def addItem(self, item):
        self.items.append(item)

    def count(self):
        return len(self.items)


class FakeDockWidget(_Recorder):
    class DockWidgetFeature:
        DockWidgetClosable = 1
        DockWidgetMovable = 2
        DockWidgetFloatable = 4

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._visible = False
        self.widget = None
        self.raised = 0

    def setWidget(self, widget):
        self.widget = widget

    def show(self):
        self._visible = True

    def hide(self):
        self._visible = False

    def isVisible(self):
        return self._visible

    def raise_(self):
        self.raised += 1


class FakeQt:
    class DockWidgetArea:
        LeftDockWidgetArea = 1
        RightDockWidgetArea = 2
