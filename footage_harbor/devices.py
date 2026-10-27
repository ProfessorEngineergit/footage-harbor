"""Card reader detection and mounting.

Two backends:
  LinuxBackend  – real hardware via lsblk/udevadm/mount. Cards are mounted
                  read-only and only remounted writable for the cleanup step.
  FolderBackend – every sub-folder of a directory is treated as an inserted
                  card. Used for development and tests on any OS.
"""
import hashlib
import json
import os
import subprocess

CARD_FS = {"vfat", "exfat", "ntfs", "ntfs3", "ext4", "ext3", "ext2", "hfsplus"}
SYSTEM_MOUNTS = {"/", "/boot", "/boot/firmware", "/home", "/var", "/usr"}


def _run(cmd, timeout=20):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class Media:
    """A detected card (partition) in a reader slot."""

    def __init__(self, slot_key, dev, label, fs_uuid, fstype, size, model="", mountpoint=None):
        self.slot_key = slot_key
        self.dev = dev
        self.label = label or ""
        self.fs_uuid = fs_uuid or ""
        self.fstype = fstype or ""
        self.size = int(size or 0)
        self.model = model or ""
        self.mountpoint = mountpoint
        self.external_mount = bool(mountpoint)

    @property
    def key(self):
        return f"{self.slot_key}|{self.dev}|{self.fs_uuid}"

    def fingerprint(self):
        raw = f"{self.fs_uuid}|{self.label}|{self.size}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


class FolderBackend:
    name = "folder"

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def scan(self):
        """Returns (slot_keys_present, media_list)."""
        media = []
        for entry in sorted(os.listdir(self.root)):
            p = os.path.join(self.root, entry)
            if entry.startswith(".") or not os.path.isdir(p):
                continue
            size = 0
            for dp, _, fns in os.walk(p):
                for fn in fns:
                    try:
                        size += os.path.getsize(os.path.join(dp, fn))
                    except OSError:
                        pass
            uuid = hashlib.md5(entry.encode()).hexdigest()[:8].upper()
            media.append(Media(f"folder:{entry}", p, entry, uuid, "folder", size, "Ordner", p))
        return media

    def empty_reader_slots(self):
        return []

    def mount(self, m, mountpoint):
        m.mountpoint = m.dev
        return m.dev

    def set_writable(self, m, writable):
        pass

    def unmount(self, m, power_off=False):
        pass

    def sync(self, m):
        pass


class LinuxBackend:
    name = "linux"

    def __init__(self, protected_paths):
        self.protected_paths = protected_paths  # callable -> list of paths
        self._idpath_cache = {}

    def _id_path(self, dev):
        if dev in self._idpath_cache:
            return self._idpath_cache[dev]
        val = ""
        try:
            out = _run(["udevadm", "info", "-q", "property", "-n", dev]).stdout
            props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            val = props.get("ID_PATH") or props.get("ID_SERIAL") or ""
        except Exception:
            pass
        self._idpath_cache[dev] = val or dev
        return self._idpath_cache[dev]

    def _lsblk(self):
        out = _run(["lsblk", "-J", "-b", "-o",
                    "NAME,PATH,LABEL,UUID,FSTYPE,SIZE,RM,HOTPLUG,TRAN,MOUNTPOINT,MODEL,TYPE"]).stdout
        return json.loads(out or '{"blockdevices":[]}').get("blockdevices", [])

    def _is_protected(self, mountpoint):
        if not mountpoint:
            return False
        if mountpoint in SYSTEM_MOUNTS:
            return True
        for p in self.protected_paths():
            p = os.path.realpath(p)
            if p == mountpoint or p.startswith(mountpoint.rstrip("/") + "/"):
                return True
        return False

    def _walk(self):
        """Yields (disk, partitions) for removable/usb/mmc disks that hold no system data."""
        for disk in self._lsblk():
            if disk.get("type") != "disk":
                continue
            removable = disk.get("rm") in (True, "1", 1) or disk.get("hotplug") in (True, "1", 1)
            if not (removable or disk.get("tran") in ("usb", "mmc")):
                continue
            # mmcblk0 on a Pi is the boot card, never a source
            parts = disk.get("children") or []
            mounts = [disk.get("mountpoint")] + [c.get("mountpoint") for c in parts]
            if any(self._is_protected(mp) for mp in mounts if mp):
                continue
            yield disk, parts

    def scan(self):
        self._idpath_cache.clear()
        media = []
        for disk, parts in self._walk():
            slot_key = self._id_path(disk["path"])
            candidates = parts if parts else [disk]
            for c in candidates:
                if (c.get("fstype") or "") not in CARD_FS:
                    continue
                media.append(Media(slot_key, c["path"], c.get("label"), c.get("uuid"),
                                   c.get("fstype"), c.get("size"), disk.get("model") or "",
                                   c.get("mountpoint")))
                break  # one card per slot: first usable partition (DCIM lives there)
        return media

    def empty_reader_slots(self):
        """Multi-slot readers expose empty slots as 0-byte disks."""
        slots = []
        for disk, _ in self._walk():
            if int(disk.get("size") or 0) == 0:
                slots.append((self._id_path(disk["path"]), disk.get("model") or ""))
        return slots

    def mount(self, m, mountpoint):
        if m.external_mount and m.mountpoint:
            return m.mountpoint
        os.makedirs(mountpoint, exist_ok=True)
        fstype = {"ntfs": "ntfs3"}.get(m.fstype, m.fstype)
        opts = "ro,noexec,nodev,nosuid,noatime"
        if fstype in ("vfat", "exfat", "ntfs3"):
            opts += ",uid=0,gid=0,fmask=0022,dmask=0022"
        r = _run(["mount", "-t", fstype, "-o", opts, m.dev, mountpoint])
        if r.returncode != 0 and fstype == "ntfs3":
            r = _run(["mount", "-t", "ntfs-3g", "-o", "ro,noexec,nodev,nosuid", m.dev, mountpoint])
        if r.returncode != 0:
            raise OSError(r.stderr.strip() or "mount failed")
        m.mountpoint = mountpoint
        return mountpoint

    def set_writable(self, m, writable):
        if m.external_mount:
            return
        mode = "rw" if writable else "ro"
        r = _run(["mount", "-o", f"remount,{mode}", m.mountpoint])
        if r.returncode != 0:
            raise OSError(r.stderr.strip() or f"remount {mode} failed")

    def sync(self, m):
        _run(["sync", "-f", m.mountpoint] if m.mountpoint else ["sync"])

    def unmount(self, m, power_off=False):
        if m.mountpoint and not m.external_mount:
            _run(["sync"])
            _run(["umount", m.mountpoint])
            try:
                os.rmdir(m.mountpoint)
            except OSError:
                pass
        if power_off:
            # Single-slot USB readers can be powered off; multi-slot readers just stay idle.
            _run(["udisksctl", "power-off", "-b", m.dev, "--no-user-interaction"], timeout=10)


def make_backend(cards_dir, protected_paths):
    if cards_dir:
        return FolderBackend(cards_dir)
    return LinuxBackend(protected_paths)


def free_space(path):
    try:
        st = os.statvfs(path)
        return {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
    except (OSError, AttributeError):
        return {"total": 0, "free": 0}


def local_disks():
    """Mounted filesystems that could hold the archive (for the setup picker)."""
    out = []
    try:
        data = json.loads(_run(["lsblk", "-J", "-b", "-o",
                                "PATH,LABEL,FSTYPE,SIZE,MOUNTPOINT,MODEL,TRAN"]).stdout)
        def walk(nodes, model=""):
            for n in nodes:
                mp = n.get("mountpoint")
                if mp and mp not in ("/boot", "/boot/firmware", "[SWAP]"):
                    out.append({"path": mp, "label": n.get("label") or model or n.get("path"),
                                "size": int(n.get("size") or 0), **free_space(mp)})
                walk(n.get("children") or [], n.get("model") or model)
        walk(data.get("blockdevices", []))
    except Exception:
        pass
    return out
