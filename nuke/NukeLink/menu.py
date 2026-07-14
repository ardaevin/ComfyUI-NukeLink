"""NukeLink menu: starts the ComfyUI -> Nuke listener and adds the send command.

Nuke runs this automatically for any folder on the plugin path (see init.py), so
there is nothing to paste into your own menu.py.
"""
import nuke

import sendToComfyUI

# Keyboard shortcut for "Send To ComfyUI". Empty by default so it cannot collide
# with an existing binding; set it to e.g. "ctrl+shift+c" if you want one.
# shortcutContext=2 scopes the shortcut to the Node Graph (DAG).
MENU_SHORTCUT = ""

# The listener is only useful in the GUI - it is what lets ComfyUI push finished
# renders back into the node graph as Read nodes.
if nuke.GUI:
    sendToComfyUI.start_listener()

    _menu = nuke.menu("Nodes").addMenu("SendToComfyUI", "")
    _menu.addCommand(
        "Send To ComfyUI",
        "sendToComfyUI.send_to_comfyui()",
        MENU_SHORTCUT,
        shortcutContext=2,
    )
