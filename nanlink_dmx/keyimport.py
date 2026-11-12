"""Pull mesh keys out of the Nanlink Android app's log output.

The app (built on the FeasyMesh SDK) logs its whole mesh network when it
starts, including the NetKey and every node, and logs the AppKey whenever it
sends a vendor message. We read that with `adb logcat -d`.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field

from . import mesh

NET_RE = re.compile(r"MeshNetwork\{meshUUID='([^']+)'.*?ivIndex=IV Index: (\d+).*?netKeys=\[NetworkKey\{.*?key=(\[[-\d, ]+\])")
NODE_RE = re.compile(r"ProvisionedMeshNode\{[^{}]*?nodeName='([^']*)'.*?meshUuid='([^']+)', uuid='([^']+)'"
                     r".*?unicastAddress=(\d+)")
APPKEY_RE = re.compile(r"MeshTransport: Key: ([0-9A-Fa-f]{32})\s*\n[^\n]*MeshTransport: akf: 1")


@dataclass
class Node:
    name: str
    uuid: str
    address: int

    def matches_mac(self, mac: str) -> bool:
        return bool(mac) and self.uuid.replace("-", "").lower().startswith(mac.replace(":", "").lower())


@dataclass
class Network:
    uuid: str
    net_key: bytes
    iv_index: int
    nodes: dict[str, Node] = field(default_factory=dict)

    @property
    def network_id(self) -> bytes:
        return mesh.k3(self.net_key)


@dataclass
class ImportResult:
    networks: dict[str, Network]
    app_keys: list[bytes]

    def find_box(self, mac: str) -> tuple[Network, Node] | None:
        for net in self.networks.values():
            for node in net.nodes.values():
                if node.matches_mac(mac):
                    return net, node
        return None


def parse_log(text: str) -> ImportResult:
    networks: dict[str, Network] = {}
    for line in text.splitlines():
        m = NET_RE.search(line)
        if not m:
            continue
        uuid = m.group(1)
        net = Network(uuid, mesh.parse_key(m.group(3)), int(m.group(2)))
        # Later dumps reflect the latest state, so they replace earlier ones
        for n in NODE_RE.finditer(line):
            if n.group(2) == uuid:
                net.nodes[n.group(3).lower()] = Node(n.group(1), n.group(3).lower(), int(n.group(4)))
        networks[uuid] = net
    app_keys: list[bytes] = []
    for m in APPKEY_RE.finditer(text):
        k = bytes.fromhex(m.group(1))
        if k not in app_keys:
            app_keys.append(k)
    return ImportResult(networks, app_keys)


def find_adb() -> str | None:
    found = shutil.which("adb")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe")]
    candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Google.PlatformTools*",
                                         "platform-tools", "adb.exe"))
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def read_phone_log(adb: str) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    devices = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=20,
                             creationflags=flags).stdout
    lines = [l for l in devices.splitlines()[1:] if l.strip()]
    if not lines:
        raise RuntimeError("No Android phone found. Connect it by USB with USB debugging enabled.")
    if all("unauthorized" in l for l in lines):
        raise RuntimeError("The phone has not authorized this computer. Unlock it and accept "
                           "'Allow USB debugging'.")
    out = subprocess.run([adb, "logcat", "-d"], capture_output=True, timeout=60,
                         creationflags=flags).stdout
    return out.decode("utf-8", "replace").replace("\r\n", "\n")


def read_file(path: str) -> str:
    """A saved logcat text file or an Android bugreport .zip."""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".txt") and "bugreport" in n.lower()]
            names = names or [n for n in z.namelist() if n.endswith(".txt")]
            return "\n".join(z.read(n).decode("utf-8", "replace") for n in names)
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()
