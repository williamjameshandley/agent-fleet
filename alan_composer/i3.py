import json
import subprocess


def _command(window_id, command):
    subprocess.run(
        ["i3-msg", "--quiet", f'[id="{window_id}"]', command], check=True)


def place(window_id):
    _command(window_id,
             "move container to workspace current, floating disable, move up")
    while True:
        tree = json.loads(subprocess.check_output(["i3-msg", "-t", "get_tree"]))
        rect, workspace = _location(tree, window_id)
        if rect["width"] == workspace["width"]:
            return
        _command(window_id, "move up")


def _location(node, window_id, workspace=None):
    if node.get("type") == "workspace":
        workspace = node["rect"]
    if node.get("window") == window_id:
        return node["rect"], workspace
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        if location := _location(child, window_id, workspace):
            return location
    return None


def _rect(node, window_id):
    if node.get("window") == window_id:
        return node["rect"]
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        if rect := _rect(child, window_id):
            return rect
    return None


def resize(window_id, height):
    tree = json.loads(subprocess.check_output(["i3-msg", "-t", "get_tree"]))
    current = _rect(tree, window_id)["height"]
    if current != height:
        direction = "shrink" if current > height else "grow"
        _command(window_id, f"resize {direction} height {abs(current - height)} px")
