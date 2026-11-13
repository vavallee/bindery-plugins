from calibre.customize import InterfaceActionBase


class BinderyBridge(InterfaceActionBase):
    name = "Bindery Bridge"
    description = (
        "Exposes a versioned HTTP API that lets Bindery add and update books "
        "in the running Calibre library without shelling out to calibredb, "
        "or fetches queued books from Bindery in pull mode."
    )
    supported_platforms = ["windows", "osx", "linux"]
    author = "vavallee"
    # The one place the version is written. plugin/handlers.py derives
    # PLUGIN_VERSION from it, and scripts/ read it for the zip name and the
    # release checks.
    version = (0, 8, 4)
    minimum_calibre_version = (6, 0, 0)

    actual_plugin = "calibre_plugins.bindery_bridge.plugin:BinderyBridgeAction"

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.bindery_bridge.plugin.config import ConfigWidget

        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.commit()
