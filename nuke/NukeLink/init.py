"""NukeLink - Nuke side of the ComfyUI-NukeLink bridge.

Nuke runs this automatically for any folder on the plugin path, so putting this
folder on NUKE_PATH (or calling nuke.pluginAddPath on it) is the whole install.
Adding the folder to the plugin path is also what makes `import sendToComfyUI`
resolve from menu.py.
"""
import os

import nuke

nuke.pluginAddPath(os.path.dirname(os.path.abspath(__file__)))
