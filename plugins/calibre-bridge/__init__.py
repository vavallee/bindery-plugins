from calibre.customize import InterfaceActionBase


class BinderyBridge(InterfaceActionBase):
    name = "Bindery Bridge"
    description = (
        "Exposes a versioned HTTP API that lets Bindery add imported books "
        "into the running Calibre library without shelling out to calibredb."
    )
    supported_platforms = ["windows", "osx", "linux"]
    author = "vavallee"
    version = (0, 3, 1)
    minimum_calibre_version = (6, 0, 0)

    actual_plugin = "calibre_plugins.bindery_bridge.plugin:BinderyBridgeAction"

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.bindery_bridge.plugin.config import ConfigWidget

        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.commit()
