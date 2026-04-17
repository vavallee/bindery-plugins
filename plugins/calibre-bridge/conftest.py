"""Stub out calibre and Qt before pytest imports the plugin package __init__.py."""
import sys
import types


def _make_module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# calibre stubs
calibre = _make_module('calibre')
customize = _make_module('calibre.customize')
customize.InterfaceActionBase = object

utils = _make_module('calibre.utils')
utils_config = _make_module('calibre.utils.config')


class _JSONConfig(dict):
    def __init__(self, name):
        super().__init__()
        self.defaults = {}

    def get(self, key, default=None):
        return super().get(key, self.defaults.get(key, default))


utils_config.JSONConfig = _JSONConfig

gui2 = _make_module('calibre.gui2')
gui2_actions = _make_module('calibre.gui2.actions')
gui2_actions.InterfaceAction = object

ebooks = _make_module('calibre.ebooks')
metadata = _make_module('calibre.ebooks.metadata')
meta = _make_module('calibre.ebooks.metadata.meta')
meta.get_metadata = lambda f, fmt: types.SimpleNamespace(title='Stub')

constants = _make_module('calibre.constants')
constants.numeric_version = (9, 0, 0)

# Qt stub (qt.core)
qt = _make_module('qt')
qt_core = _make_module('qt.core')
for _cls in ('QDialog', 'QDialogButtonBox', 'QVBoxLayout', 'QFormLayout',
             'QLineEdit', 'QSpinBox', 'QWidget'):
    setattr(qt_core, _cls, object)
