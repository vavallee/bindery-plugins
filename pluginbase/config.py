"""Base Qt config widget mixin for Calibre plugin configuration dialogs."""

from __future__ import annotations

from typing import Any


class BaseConfigWidget:
    """Mixin providing :meth:`commit` / :meth:`load` contract for config widgets.

    Subclass this alongside a Qt widget base (e.g. ``QWidget``) and implement
    :meth:`_save_values` and :meth:`_load_values`.  The Calibre plugin framework
    calls :meth:`commit` when the user clicks OK.

    Example::

        class ConfigWidget(BaseConfigWidget, QWidget):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._load_values()

            def _load_values(self):
                self.port_input.setValue(prefs.get("port", 8099))

            def _save_values(self):
                prefs["port"] = self.port_input.value()
    """

    def commit(self) -> None:
        """Persist widget values to the backing store."""
        self._save_values()

    def _save_values(self) -> None:
        raise NotImplementedError  # pragma: no cover

    def _load_values(self) -> None:
        raise NotImplementedError  # pragma: no cover
