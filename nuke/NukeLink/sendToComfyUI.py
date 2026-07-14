import nuke
import nukescripts
import os
import re
import json
import socket
import threading

try:
    import urllib2
    def http_post(url, payload):
        req = urllib2.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        response = urllib2.urlopen(req)
        return response.read()
except ImportError:
    import urllib.request
    def http_post(url, payload):
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        response = urllib.request.urlopen(req)
        return response.read()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
COMFYUI_HOST = "http://127.0.0.1:8188"

# Path Builder output location is derived from the Nuke script's location.
# LEVELS_UP controls how many folders to climb from the .nk file.
# OUTPUT_SUBFOLDER is the folder path appended after climbing. It may be a
# nested path using forward slashes (e.g. "elements" or "Comp/Inputs/ComfyUI").
#
# Example - script at:
#   E:\Shows\BigProject\Shots\BP_303_003\nuke\scripts\BP_303_003_comp_v001.nk
# With LEVELS_UP = 2 and OUTPUT_SUBFOLDER = "elements":
#   E:\Shows\BigProject\Shots\BP_303_003\elements\
#
# If your scripts folder sits one level deep (no "scripts" subfolder):
#   E:\Shows\BigProject\Shots\BP_303_003\nuke\BP_303_003_comp_v001.nk
# Set LEVELS_UP = 1 instead.

LEVELS_UP = 2
OUTPUT_SUBFOLDER = "elements"
LISTENER_PORT_START = 54321
LISTENER_PORT_RANGE = 10

# USE_PROJECT_DIRECTORY
# Many pipelines set the Root node's "project_directory" to the shot folder, so
# Read nodes hold paths relative to it. When True, that folder is used as the
# shot root for the output location instead of climbing LEVELS_UP from the .nk.
# If project_directory is empty (or this is False), the LEVELS_UP climb is used,
# so behaviour is unchanged for setups that do not use a project root.
#
# Leave this False if your project_directory points at the SHOW root rather than
# the shot, otherwise outputs would land at the show level.
#
# NOTE: relative Read paths are ALWAYS resolved to absolute before sending,
# regardless of this flag - ComfyUI has no concept of Nuke's project directory.
USE_PROJECT_DIRECTORY = False

# Shot name derivation
# Set SHOT_ENV_VAR to the environment variable your pipeline uses for shot name.
# Set SHOT_VERSION_SEPARATOR to the string that separates shot name from the rest
# of the script filename (e.g. "_v", "-v", "_comp_v", "_nukeScript_v").
SHOT_ENV_VAR = "SHOT"
SHOT_VERSION_SEPARATOR = "_comp_v"

# Send dialog
# DEFAULT_WORKFLOWS_FOLDER seeds the "Workflows Folder" field on first run; leave
# it empty to just browse to your folder the first time. After that, the folder
# and output subpath you last used are restored from PREFS_FILE.
#
# Point it at any folder of saved ComfyUI workflow .json files, e.g.
#   <ComfyUI>/user/default/workflows
# Every .json in there becomes an entry in the Workflow dropdown. To qualify as a
# template a workflow must contain a "Read - NukeLink" node (that is where the
# plate is injected); a "Path Builder - NukeLink" is recommended.
#
# Dropdown contents:
#   DEFAULT_LABEL -> builds DEFAULT_TEMPLATE_FILE, your own default setup.
#                    Always listed first and pre-selected. Save a workflow to
#                    that path to define it.
#   anything else -> a .json from the Workflows Folder.
#
# BARE_LABEL is the stock NukeLink behaviour (Read node(s) + Path Builder only,
# no template). It is hidden from the dropdown, but kept on purpose: it is still
# the automatic fallback when DEFAULT_TEMPLATE_FILE is missing or unreadable, so
# a fresh install still sends something useful. Set SHOW_BARE_OPTION = True to
# offer it as an explicit choice.
#
# The Workflow dropdown is not persisted; it always reopens on DEFAULT_LABEL.
DEFAULT_WORKFLOWS_FOLDER = ""
PREFS_FILE = os.path.expanduser("~/.nuke/nukelink_send_prefs.json")
DEFAULT_TEMPLATE_FILE = os.path.expanduser("~/.nuke/nukelink_default_template.json")
DEFAULT_LABEL = "Default"
BARE_LABEL = "(bare)"
SHOW_BARE_OPTION = False
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {
    "exr", "tiff", "tif", "dpx", "hdr", "png", "jpeg", "jpg",
    "bmp", "tga", "psd", "ico", "rla", "sgi", "pnm", "ppm", "pgm",
    "pbm", "webp", "gif", "heic", "jp2", "jxr", "pic", "pcx", "im", "dib"
}

ALLOWED_COLORSPACES = {"raw", "sRGB", "linear", "ACEScg"}

FRAME_PATTERN_RE = re.compile(r'%0\d+d|#+')


def _is_abs_path(path):
    """True for drive-letter, UNC and posix-absolute paths."""
    if not path:
        return False
    return (
        os.path.isabs(path)
        or path.startswith("//")
        or re.match(r'^[A-Za-z]:[/\\]', path) is not None
    )


def _project_directory():
    """Root.project_directory, evaluated - it commonly holds a TCL expression
    such as [file dirname [value root.name]]. Returns '' when unset."""
    try:
        value = nuke.root()["project_directory"].evaluate() or ""
    except Exception:
        value = ""
    return value.replace("\\", "/").strip().rstrip("/")


def _script_directory():
    script_path = nuke.root().name()
    if not script_path or script_path == "Root":
        return ""
    return os.path.dirname(os.path.abspath(script_path)).replace("\\", "/")


def _read_file_value(node):
    """The Read's file path, with any TCL/env expression expanded but the frame
    pattern (%04d / ####) preserved."""
    raw = (node["file"].value() or "").strip()
    if not raw or ("[" not in raw and "$" not in raw):
        return raw.replace("\\", "/")
    # Only reach for nuke.filename() when there is actually an expression to
    # expand, and never accept a result that ate the frame pattern.
    try:
        expanded = (nuke.filename(node) or "").strip()
    except Exception:
        expanded = ""
    if expanded:
        had = FRAME_PATTERN_RE.search(raw) is not None
        keeps = FRAME_PATTERN_RE.search(expanded) is not None
        if not had or keeps:
            raw = expanded
    return raw.replace("\\", "/")


def _resolve_read_path(raw):
    """Make a Read path absolute. Relative paths are resolved against
    project_directory, falling back to the .nk script's folder - the same rule
    Nuke itself applies. Absolute paths are returned untouched."""
    if not raw:
        return ""
    path = raw.replace("\\", "/")
    if _is_abs_path(path):
        return path
    base = _project_directory() or _script_directory()
    if not base:
        return ""  # nothing to resolve against - caller reports it
    return os.path.normpath(os.path.join(base, path)).replace("\\", "/")


def _derive_file_location(subfolder=None):
    """Build the output location: <shot root>/<subfolder>/.

    Shot root is project_directory when USE_PROJECT_DIRECTORY is on and it is
    set; otherwise climb LEVELS_UP folders from the .nk script."""
    if subfolder is None:
        subfolder = OUTPUT_SUBFOLDER
    subfolder = subfolder.replace("\\", "/").strip().lstrip("/")

    base = ""
    if USE_PROJECT_DIRECTORY:
        base = _project_directory()

    if not base:
        script_dir = _script_directory()
        if not script_dir:
            return ""
        base = script_dir
        for _ in range(LEVELS_UP):
            parent = os.path.dirname(base)
            if parent == base:
                break
            base = parent

    return os.path.join(base, subfolder, "").replace("\\", "/")


def _derive_version(script_path):
    """Extract version digits from the .nk filename. Returns e.g. '001' or '01'. Fallback: '01'."""
    filename = os.path.basename(script_path)
    match = re.search(r'v(\d{2,3})(?=\D|$)', filename, re.IGNORECASE)
    if match:
        return match.group(1)
    return "01"


def _derive_frame_delim(file_path):
    """Extract the character immediately before the frame pattern in the file path.
    Handles #### and %04d style patterns. Returns None if not found (leave widget default)."""
    match = re.search(r'(.)(?:#+|%\d+d)', file_path)
    if match:
        return match.group(1)
    return None


def _derive_shot(script_path):
    """Derive shot name from environment variable or script filename."""
    env_val = os.environ.get(SHOT_ENV_VAR, "").strip()
    if env_val:
        return env_val
    if not script_path or script_path == "Root":
        return ""
    filename = os.path.splitext(os.path.basename(script_path))[0]
    sep = SHOT_VERSION_SEPARATOR
    idx = filename.lower().find(sep.lower())
    if idx > 0:
        return filename[:idx]
    return ""


# ---------------------------------------------------------------------------
# Send dialog: workflows folder + output subpath + template picker
# ---------------------------------------------------------------------------

def _load_prefs():
    try:
        with open(PREFS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_prefs(prefs):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f, indent=2)
    except Exception as e:
        print("[NukeLink] could not save prefs: {}".format(str(e)))


def _scan_workflows(folder):
    """Return sorted workflow names (.json files, extension stripped) in folder."""
    try:
        names = [
            os.path.splitext(x)[0]
            for x in os.listdir(folder)
            if x.lower().endswith(".json")
        ]
        return sorted(names, key=lambda s: s.lower())
    except Exception:
        return []


class SendToComfyUIPanel(nukescripts.PythonPanel):
    """Modal send dialog. OK acts as the Send button.

    Enumeration knobs mishandle spaces in item labels, so workflow names are
    shown with spaces replaced by underscores; _wf_map maps label -> real name.
    """

    def __init__(self):
        nukescripts.PythonPanel.__init__(self, "Send To ComfyUI")

        prefs = _load_prefs()
        folder = prefs.get("workflows_folder", DEFAULT_WORKFLOWS_FOLDER)
        subpath = prefs.get("output_subpath", OUTPUT_SUBFOLDER)

        self.folderKnob = nuke.File_Knob("wf_folder", "Workflows Folder")
        self.folderKnob.setValue(folder.replace("\\", "/"))
        self.addKnob(self.folderKnob)

        self.subpathKnob = nuke.String_Knob("out_subpath", "Output Subpath")
        self.subpathKnob.setValue(subpath)
        self.subpathKnob.setTooltip(
            "Appended to the shot root to build the output location, "
            "e.g. elements/ or Comp/Inputs/ComfyUI/"
        )
        self.addKnob(self.subpathKnob)

        self.workflowKnob = nuke.Enumeration_Knob("workflow", "Workflow", [DEFAULT_LABEL])
        self.workflowKnob.setTooltip(
            "'{}' builds your default template ({}). Save a workflow to that path "
            "to define it.\n"
            "Anything else is a .json from the Workflows Folder.".format(
                DEFAULT_LABEL, DEFAULT_TEMPLATE_FILE
            )
        )
        self.addKnob(self.workflowKnob)

        self._wf_map = {}
        self._rescan()

    def _current_folder(self):
        val = (self.folderKnob.value() or "").strip().replace("\\", "/")
        # File_Knob browses files; accept a .json pick as "its folder"
        if val.lower().endswith(".json") and os.path.isfile(val):
            val = os.path.dirname(val)
        return val.rstrip("/")

    def _rescan(self):
        """Rebuild the dropdown from the folder. Always lands on DEFAULT_LABEL."""
        names = _scan_workflows(self._current_folder())
        self._wf_map = {}
        # BARE_LABEL stays reserved even while hidden, so a workflow file of the
        # same name can never shadow it.
        reserved = (DEFAULT_LABEL, BARE_LABEL)
        labels = [DEFAULT_LABEL]
        if SHOW_BARE_OPTION:
            labels.append(BARE_LABEL)
        for name in names:
            label = name.replace(" ", "_")
            while label in self._wf_map or label in reserved:
                label += "_"
            self._wf_map[label] = name
            labels.append(label)
        self.workflowKnob.setValues(labels)
        self.workflowKnob.setValue(DEFAULT_LABEL)

    def knobChanged(self, knob):
        if knob is not None and knob.name() == "wf_folder":
            self._rescan()

    def selection(self):
        label = self.workflowKnob.value()
        if label == DEFAULT_LABEL:
            mode, workflow = "default", None
        elif label == BARE_LABEL:
            mode, workflow = "bare", None
        else:
            mode, workflow = "folder", self._wf_map.get(label)
        return {
            "folder": self._current_folder(),
            "subpath": (self.subpathKnob.value() or "").strip(),
            "mode": mode,
            "workflow": workflow,
        }


def send_to_comfyui():
    selected = nuke.selectedNodes()
    if not selected:
        nuke.message("No node selected.\n\nPlease select one or more Read nodes.")
        return

    read_nodes = [n for n in selected if n.Class() == "Read"]
    if not read_nodes:
        nuke.message("No Read nodes in selection.\n\nPlease select one or more Read nodes.")
        return

    script_path = nuke.root().name()
    version_number = _derive_version(script_path) if script_path and script_path != "Root" else "01"

    reads = []
    skipped = []

    for node in read_nodes:
        raw_path = _read_file_value(node)
        if not raw_path:
            skipped.append("{} (no file path)".format(node.name()))
            continue

        # ComfyUI has no notion of Nuke's project directory, so a Read holding a
        # pipeline-relative path must be made absolute before it leaves Nuke.
        file_path = _resolve_read_path(raw_path)
        if not file_path or not _is_abs_path(file_path):
            skipped.append(
                "{} (relative path could not be resolved: {})".format(node.name(), raw_path)
            )
            continue

        ext = os.path.splitext(file_path)[1].lstrip(".").lower()
        if ext not in ALLOWED_EXTENSIONS:
            skipped.append("{} (unsupported type .{})".format(node.name(), ext))
            continue

        if node["raw"].value():
            colorspace = "raw"
        else:
            colorspace_val = node["colorspace"].value()
            if colorspace_val in ALLOWED_COLORSPACES:
                colorspace = colorspace_val
            else:
                cs_lower = colorspace_val.lower()
                if "acescg" in cs_lower:
                    colorspace = "ACEScg"
                elif "srgb" in cs_lower:
                    colorspace = "sRGB"
                elif "linear" in cs_lower:
                    colorspace = "linear"
                else:
                    colorspace = "raw"

        first_frame    = int(node["first"].value())
        last_frame     = int(node["last"].value())
        missing_frames = node["on_error"].value()
        if missing_frames not in {"error", "black", "hold", "nearest"}:
            missing_frames = "black"

        reads.append({
            "file_path":      file_path,
            "first_frame":    first_frame,
            "last_frame":     last_frame,
            "colorspace":     colorspace,
            "missing_frames": missing_frames,
        })

    if not reads:
        nuke.message("No valid Read nodes to send.")
        return

    if not _listener_port:
        nuke.message("NukeLink listener is not running.\n\nRestart Nuke and try again.")
        return

    # Send dialog: workflows folder, output subpath, template picker.
    panel = SendToComfyUIPanel()
    if not panel.showModalDialog():
        return  # cancelled
    sel = panel.selection()

    # Folder and subpath persist; the workflow choice deliberately does not -
    # the dropdown always reopens on DEFAULT_LABEL.
    prefs = _load_prefs()
    prefs["workflows_folder"] = sel["folder"]
    prefs["output_subpath"] = sel["subpath"]
    prefs.pop("last_workflow", None)
    _save_prefs(prefs)

    file_location = _derive_file_location(sel["subpath"] or OUTPUT_SUBFOLDER)

    template_name = None
    template_workflow = None

    if sel["mode"] == "default":
        try:
            with open(DEFAULT_TEMPLATE_FILE, "r") as f:
                template_workflow = json.load(f)
            template_name = DEFAULT_LABEL
        except Exception as e:
            # Don't block the send - fall back to the bare Read + Path Builder.
            nuke.message(
                "Default template could not be loaded, sending bare "
                "(Read + Path Builder only):\n{}\n\n{}".format(DEFAULT_TEMPLATE_FILE, str(e))
            )
    elif sel["mode"] == "folder" and sel["workflow"]:
        wf_path = os.path.join(sel["folder"], sel["workflow"] + ".json")
        try:
            with open(wf_path, "r") as f:
                template_workflow = json.load(f)
            template_name = sel["workflow"]
        except Exception as e:
            nuke.message("Could not load workflow template:\n{}\n\n{}".format(wf_path, str(e)))
            return
    # mode == "bare" -> no template; stock Read + Path Builder only.

    frame_delim = None
    for r in reads:
        frame_delim = _derive_frame_delim(r["file_path"])
        if frame_delim:
            break

    shot = _derive_shot(script_path)

    payload = json.dumps({
        "reads": reads,
        "file_location": file_location,
        "version_number": version_number,
        "frame_delim": frame_delim,
        "shot": shot,
        "send_path_builder": True,
        "nuke_port": _listener_port,
        "template_name": template_name,
        "template_workflow": template_workflow,
    }).encode("utf-8")

    try:
        http_post(COMFYUI_HOST + "/nukelink/receive", payload)
        msg = "{} Read node{} sent to ComfyUI. Switch to ComfyUI to see the node{} on the canvas.".format(
            len(reads),
            "s" if len(reads) != 1 else "",
            "s" if len(reads) != 1 else "",
        )
        if template_name:
            msg += "\n\nTemplate: {}".format(template_name)
        if skipped:
            msg += "\n\nSkipped:\n" + "\n".join(skipped)
        nuke.message(msg)
    except Exception as e:
        nuke.message("Failed to send to ComfyUI:\n{}".format(str(e)))

# ---------------------------------------------------------------------------
# ComfyUI -> Nuke listener
# ---------------------------------------------------------------------------

def _create_read_node(params, result_holder):
    try:
        n = nuke.nodes.Read(
            file=params.get("file_path", ""),
            first=params.get("first_frame", 1001),
            last=params.get("last_frame", 1001),
        )
        colorspace = params.get("colorspace")
        if colorspace:
            try:
                if colorspace == "raw":
                    n["raw"].setValue(True)
                else:
                    n["colorspace"].setValue(colorspace)
            except Exception:
                pass
        result_holder["result"] = "OK"
    except Exception as e:
        result_holder["result"] = "ERROR: {}".format(str(e))


def _handle_client(conn):
    try:
        data = conn.recv(65536)
        payload = json.loads(data.decode("utf-8"))

        result_holder = {}
        nuke.executeInMainThreadWithResult(_create_read_node, args=(payload, result_holder))

        conn.sendall(result_holder.get("result", "NO RESULT").encode("utf-8"))
    except Exception as e:
        try:
            conn.sendall("ERROR: {}".format(str(e)).encode("utf-8"))
        except Exception:
            pass
    finally:
        conn.close()


def _server_loop(sock):
    while True:
        conn, addr = sock.accept()
        threading.Thread(target=_handle_client, args=(conn,), daemon=True).start()

_listener_port = None

def start_listener():
    global _listener_port
    for port in range(LISTENER_PORT_START, LISTENER_PORT_START + LISTENER_PORT_RANGE):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.listen(5)
            threading.Thread(target=_server_loop, args=(s,), daemon=True).start()
            _listener_port = port
            print("[NukeLink] listener started on port {}".format(port))
            return port
        except OSError:
            continue
    print("[NukeLink] could not start listener, no available port in range")
    return None
