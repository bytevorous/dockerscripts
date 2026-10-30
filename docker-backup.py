#!/usr/bin/env python3
"""
docker-backup.py DEST - back up every container on a Docker host into a new folder under DEST.

Creates DEST/docker-backup-<host>-<timestamp>/ (never overwrites or deletes anything) containing:
  containers/<name>/inspect.json   full configuration of each container
  volumes/<name>.tar.gz            contents of every named volume used by the containers (+ .inspect.json)
  binds/<path>.tar.gz              contents of bind-mounted host folders / files
  networks/<name>.json             user-defined networks the containers use
  images/<ref>.tar                 the images, with --images (offline restore)
  reinit/*.sh                      docker run commands to recreate each container (if docker-reinit.py
                                   sits next to this script)
  manifest.json, SHA256SUMS, RESTORE.txt

Volumes are read straight from the host when readable (fast); otherwise through a throw-away helper
container that mounts the volume read-only (default image: alpine; override with --helper).
Docker sockets, /proc /sys /dev /run, devices and any folder that contains the destination are skipped.

Consistency: by default containers keep running (fine for files, risky for databases).
  --quiesce pause   freeze a container's processes while its data is copied, then unpause
  --quiesce stop    stop the container while its data is copied, then start it again
Only containers that were running are touched, and they are always restored, even on Ctrl-C.

  python3 docker-backup.py /mnt/backups --dry-run          # show the plan, change nothing
  python3 docker-backup.py /mnt/backups
  python3 docker-backup.py /mnt/backups --quiesce stop --images
  python3 docker-backup.py /mnt/backups --only moodle --only mariadb
  ssh user@server 'python3 - /mnt/backups' < docker-backup.py      # (no reinit/ folder this way)

The backup contains secrets (environment variables in inspect.json): files are created 0600, folders 0700.
Options: --dry-run  --only NAME  --images  --quiesce {none,pause,stop}  --no-binds  --running-only
         --helper IMAGE  --context NAME
"""
import argparse, contextlib, datetime, hashlib, json, os, re, shutil, socket, stat, subprocess, sys

SKIP_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/var/run")
BUILTIN_NETS = {"bridge", "host", "none"}


def make_docker(ctx):
    base = ["docker"] + (["--context", ctx] if ctx else [])

    def docker(*args, check=True):
        r = subprocess.run(base + list(args), capture_output=True, text=True)
        if r.returncode != 0:
            if check:
                sys.exit("ERROR: " + (r.stderr.strip() or "docker " + " ".join(args)))
            return None
        return r.stdout
    docker.base = base
    return docker


def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s.strip("/").replace("/", "__")) or "root"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_private(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def tar_flags():
    try:
        v = subprocess.run(["tar", "--version"], capture_output=True, text=True).stdout
    except OSError:
        return None
    return ["--numeric-owner"] if "GNU tar" in v else []


def tar_direct(flags, src_dir, out, name):
    r = subprocess.run(["tar"] + flags + ["-czpf", out, "-C", src_dir, name or "."], capture_output=True, text=True)
    return r.returncode, r.stderr.strip()


def tar_helper(docker, helper, mount_src, out, name):
    args = docker.base + ["run", "--rm", "-v", f"{mount_src}:/src:ro", helper, "tar", "czpf", "-", "-C", "/src",
                          name or "."]
    with open(out, "wb") as f:
        r = subprocess.run(args, stdout=f, stderr=subprocess.PIPE)
    return r.returncode, r.stderr.decode(errors="replace").strip()


def archive(docker, helper, flags, direct_dir, helper_mount, out, name=None):
    """Try the fast direct tar, fall back to the helper container. -> (ok, method, warning)"""
    if flags is not None and direct_dir and os.path.exists(direct_dir) and os.access(direct_dir, os.R_OK):
        rc, err = tar_direct(flags, direct_dir, out, name)
        if rc in (0, 1):
            return True, "host tar", (err or "files changed while reading") if rc == 1 else ""
        with contextlib.suppress(OSError):
            os.remove(out)
    rc, err = tar_helper(docker, helper, helper_mount, out, name)
    if rc == 0:
        return True, f"helper {helper}", ""
    with contextlib.suppress(OSError):
        os.remove(out)
    return False, "failed", err or f"tar exited {rc}"


@contextlib.contextmanager
def quiesced(docker, mode, names, running, log):
    acted = []
    try:
        if mode != "none":
            for n in names:
                if running.get(n):
                    log(f"    {mode} {n}")
                    if docker(mode, n, check=False) is not None:
                        acted.append(n)
                    else:
                        log(f"    WARNING: could not {mode} {n}")
        yield
    finally:
        for n in reversed(acted):
            verb = "unpause" if mode == "pause" else "start"
            log(f"    {verb} {n}")
            if docker(verb, n, check=False) is None:
                log(f"    WARNING: could not {verb} {n} - do it manually!")


def bind_skip_reason(src, dest_real):
    if any(src == p or src.startswith(p + "/") for p in SKIP_PREFIXES):
        return "system path"
    try:
        mode = os.stat(src).st_mode
    except OSError:
        return "not found / not accessible on this host (run as root, on the docker host)"
    if stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
        return "socket / device"
    if dest_real == os.path.realpath(src) or dest_real.startswith(os.path.realpath(src).rstrip("/") + "/"):
        return "contains the backup destination"
    return None


def du_kb(path):
    try:
        r = subprocess.run(["du", "-sk", path], capture_output=True, text=True, timeout=120)
        return int(r.stdout.split()[0]) * 1024 if r.returncode == 0 or r.stdout else None
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def hb(n):
    if n is None:
        return "unknown"
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


RESTORE = """RESTORE NOTES  ({stamp}, host {host})
================================================================================
Automatic restore (verifies, then images, networks, volumes, containers; anything that already
exists is kept unless named with --overwrite-container NAME / --overwrite-volume NAME):
    python3 docker-restore.py /path/to/this/folder --dry-run
    python3 docker-restore.py /path/to/this/folder

Manual restore, step by step:

Images (only if the backup was made with --images):
    docker load -i images/<file>.tar

Containers:
    reinit/<name>.sh contains the docker run command for each container (generated by docker-reinit.py).
    Run them in the order printed in manifest.json ("start_order") - databases before apps.
    Each of those files begins with 'docker stop X && docker rm X'; read before running.
    containers/<name>/inspect.json is the complete original configuration.

Named volumes (data is restored into an EMPTY volume):
    docker volume create VOL
    docker run --rm -v VOL:/dst -v "$PWD/volumes":/src:ro alpine sh -c 'tar xzpf /src/VOL.tar.gz -C /dst'

Bind-mounted folders (extract into the original host path, as the same user):
    tar xzpf binds/<file>.tar.gz -C /original/host/path          (folders)
    tar xzpf binds/<file>.tar.gz -C /original/host/directory     (single files: see manifest.json for the path)

Verify the files first (from inside this backup folder):   sha256sum -c SHA256SUMS
"""


def main():
    ap = argparse.ArgumentParser(description="Back up every Docker container (config + data) into DEST")
    ap.add_argument("dest", help="folder to create the backup in")
    ap.add_argument("--only", action="append", default=[], metavar="NAME", help="back up only this container (repeatable)")
    ap.add_argument("--context")
    ap.add_argument("--images", action="store_true", help="also save the images (docker save)")
    ap.add_argument("--quiesce", choices=("none", "pause", "stop"), default="none")
    ap.add_argument("--no-binds", action="store_true", help="do not back up bind-mounted host folders")
    ap.add_argument("--running-only", action="store_true")
    ap.add_argument("--helper", default="alpine", help="image used to read volumes the host cannot read (default alpine)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be backed up; write nothing")
    a = ap.parse_args()
    if not shutil.which("docker"):
        sys.exit("ERROR: docker CLI not found")
    docker = make_docker(a.context)

    ids = docker("ps", "-q" if a.running_only else "-aq").split()
    containers = json.loads(docker("container", "inspect", *ids)) if ids else []
    if a.only:
        have = {c["Name"].lstrip("/") for c in containers}
        for n in a.only:
            if n not in have:
                sys.exit(f"ERROR: no container named '{n}'")
        containers = [c for c in containers if c["Name"].lstrip("/") in a.only]
    if not containers:
        sys.exit("No containers found.")
    containers.sort(key=lambda c: c["Name"])
    host = (docker("info", "--format", "{{.Name}}", check=False) or "?").strip()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.abspath(os.path.expanduser(a.dest))
    dest_real = os.path.realpath(dest)
    outdir = os.path.join(dest, f"docker-backup-{safe(a.context or host)}-{stamp}")
    running = {c["Name"].lstrip("/"): bool(c["State"].get("Running") and not c["State"].get("Paused"))
               for c in containers}

    vols, binds = {}, {}
    nets_used = set()
    for c in containers:
        n = c["Name"].lstrip("/")
        nets_used.update(k for k in (c["NetworkSettings"].get("Networks") or {}) if k not in BUILTIN_NETS)
        for m in c.get("Mounts") or []:
            if m.get("Type") == "volume":
                v = vols.setdefault(m["Name"], {"mountpoint": m.get("Source"), "driver": m.get("Driver"), "users": []})
                v["users"].append(n)
            elif m.get("Type") == "bind" and not a.no_binds:
                b = binds.setdefault(m["Source"], {"users": [], "dests": []})
                b["users"].append(n)
                b["dests"].append(m["Destination"])

    log = lambda s: print(s, flush=True)
    flags = tar_flags()
    log(f"Docker host '{host}': {len(containers)} containers "
        f"({sum(running.values())} running, {len(containers) - sum(running.values())} not running)")
    log(f"Destination: {outdir}")
    probe_dir = dest
    while not os.path.exists(probe_dir) and os.path.dirname(probe_dir) != probe_dir:
        probe_dir = os.path.dirname(probe_dir)
    with contextlib.suppress(OSError):
        log(f"Free space at destination: {hb(shutil.disk_usage(probe_dir).free)}")

    if a.dry_run:
        log("\nDRY RUN - nothing will be written.\n")
        log("Containers (config saved):  " + ", ".join(c["Name"].lstrip("/") for c in containers))
        log(f"\nNamed volumes ({len(vols)}):")
        for name, v in sorted(vols.items()):
            mp = v["mountpoint"]
            readable = bool(mp and os.path.isdir(mp) and os.access(mp, os.R_OK | os.X_OK))
            size = du_kb(mp) if readable else None
            log(f"  {name:<30} size {hb(size):>9}   used by {', '.join(sorted(set(v['users'])))}"
                + ("" if readable else "   (read via helper container)"))
        log(f"\nBind mounts ({len(binds)}):")
        for src, b in sorted(binds.items()):
            why = bind_skip_reason(src, dest_real)
            log(f"  {src:<40} " + (f"SKIPPED: {why}" if why else f"size {hb(du_kb(src))}"))
        if a.images:
            log("\nImages would be saved with docker save.")
        if a.quiesce != "none":
            log(f"\nQuiesce mode '{a.quiesce}': running containers using each volume/bind are "
                f"{'paused' if a.quiesce == 'pause' else 'stopped'} while it is copied, then restored.")
        return

    os.makedirs(outdir, mode=0o700)
    for sub in ("containers", "volumes", "binds", "networks", "images"):
        os.makedirs(os.path.join(outdir, sub), mode=0o700, exist_ok=True)
    manifest = {"host": host, "created": stamp, "options": {"quiesce": a.quiesce, "images": a.images,
                "binds": not a.no_binds, "helper": a.helper}, "containers": [], "volumes": [], "binds": [],
                "images": [], "networks": [], "errors": [], "warnings": []}

    for c in containers:
        n = c["Name"].lstrip("/")
        d = os.path.join(outdir, "containers", safe(n))
        os.makedirs(d, mode=0o700, exist_ok=True)
        write_private(os.path.join(d, "inspect.json"), json.dumps(c, indent=2))
        manifest["containers"].append({"name": n, "image": c["Config"].get("Image"),
                                       "state": c["State"].get("Status"),
                                       "volumes": [m["Name"] for m in c.get("Mounts") or [] if m.get("Type") == "volume"],
                                       "binds": [m["Source"] for m in c.get("Mounts") or [] if m.get("Type") == "bind"]})
    log(f"\nSaved configuration of {len(containers)} containers.")
    for nn in sorted(nets_used):
        out = docker("network", "inspect", nn, check=False)
        if out:
            write_private(os.path.join(outdir, "networks", safe(nn) + ".json"), out)
            manifest["networks"].append(nn)

    here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ""
    reinit = os.path.join(here, "docker-reinit.py")
    if not a.only and here and os.path.exists(reinit):
        cmd = [sys.executable, reinit, "-o", os.path.join(outdir, "reinit")] + (["--context", a.context] if a.context else [])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            for root, _dirs, fs in os.walk(os.path.join(outdir, "reinit")):
                for fn in fs:
                    os.chmod(os.path.join(root, fn), 0o700)
            log("Generated reinit/ scripts (docker run commands).")
            manifest["start_order"] = [ln.strip().replace(".sh", "") for ln in r.stderr.splitlines()
                                       if ln.strip().endswith(".sh")]
        else:
            manifest["warnings"].append("docker-reinit.py failed: " + (r.stderr.strip().splitlines() or ["?"])[-1])

    def record(kind, name, rel, ok, method, warn, extra=None):
        path = os.path.join(outdir, rel)
        item = {"name": name, "file": rel, "method": method}
        if extra:
            item.update(extra)
        if ok:
            os.chmod(path, 0o600)
            item.update({"bytes": os.path.getsize(path), "sha256": sha256(path)})
            log(f"    ok  {hb(item['bytes'])}  ({method})")
            if warn:
                manifest["warnings"].append(f"{kind} {name}: {warn}")
                log(f"    note: {warn}")
        else:
            item["error"] = warn
            manifest["errors"].append(f"{kind} {name}: {warn}")
            log(f"    FAILED: {warn}")
        manifest[kind + "s"].append(item)

    log(f"\nVolumes ({len(vols)}):")
    for name, v in sorted(vols.items()):
        users = sorted(set(v["users"]))
        log(f"  volume {name}   (used by {', '.join(users)})")
        mp = v["mountpoint"]
        if mp and os.path.realpath(mp) != "/" and (dest_real == os.path.realpath(mp)
                                                    or dest_real.startswith(os.path.realpath(mp).rstrip("/") + "/")):
            manifest["errors"].append(f"volume {name}: destination is inside this volume")
            log("    SKIPPED: the destination is inside this volume")
            continue
        rel = os.path.join("volumes", safe(name) + ".tar.gz")
        out = os.path.join(outdir, rel)
        try:
            ij = docker("volume", "inspect", name, check=False)
            if ij:
                write_private(os.path.join(outdir, "volumes", safe(name) + ".inspect.json"), ij)
            with quiesced(docker, a.quiesce, users, running, log):
                ok, method, warn = archive(docker, a.helper, flags, mp if v["driver"] in (None, "local") else None,
                                           name, out)
        except Exception as e:                      # keep going with the remaining volumes
            ok, method, warn = False, "failed", str(e)
        record("volume", name, rel, ok, method, warn, {"used_by": users})

    if binds:
        log(f"\nBind mounts ({len(binds)}):")
    for src, b in sorted(binds.items()):
        users = sorted(set(b["users"]))
        log(f"  bind {src}   (used by {', '.join(users)})")
        why = bind_skip_reason(src, dest_real)
        if why:
            manifest["warnings"].append(f"bind {src}: skipped ({why})")
            manifest["binds"].append({"name": src, "skipped": why})
            log(f"    skipped: {why}")
            continue
        rel = os.path.join("binds", safe(src) + ".tar.gz")
        out = os.path.join(outdir, rel)
        isfile = os.path.isfile(src)
        parent, base = os.path.split(src.rstrip("/"))
        try:
            with quiesced(docker, a.quiesce, users, running, log):
                if isfile:
                    ok, method, warn = archive(docker, a.helper, flags, parent, parent, out, base)
                else:
                    ok, method, warn = archive(docker, a.helper, flags, src, src, out)
        except Exception as e:
            ok, method, warn = False, "failed", str(e)
        record("bind", src, rel, ok, method, warn, {"used_by": users, "single_file": isfile})

    if a.images:
        refs = {}
        for c in containers:
            ref = c["Config"].get("Image") or c["Image"]
            got = docker("image", "inspect", "-f", "{{.Id}}", ref, check=False)
            refs[ref if got and got.strip() == c["Image"] else c["Image"]] = None
        log(f"\nImages ({len(refs)}):")
        for ref in sorted(refs):
            log(f"  image {ref}")
            rel = os.path.join("images", safe(ref) + ".tar")
            ok = docker("save", "-o", os.path.join(outdir, rel), ref, check=False) is not None
            record("image", ref, rel, ok, "docker save", "" if ok else "docker save failed")

    write_private(os.path.join(outdir, "manifest.json"), json.dumps(manifest, indent=2))
    write_private(os.path.join(outdir, "RESTORE.txt"), RESTORE.format(stamp=stamp, host=host))
    sums = []
    for root, _dirs, fs in os.walk(outdir):
        for fn in sorted(fs):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, outdir)
            if rel != "SHA256SUMS":
                sums.append(f"{sha256(full)}  {rel}")
    write_private(os.path.join(outdir, "SHA256SUMS"), "\n".join(sorted(sums, key=lambda s: s.split("  ", 1)[1])) + "\n")

    total = sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(outdir) for f in fs)
    log(f"\nDone: {outdir}  ({hb(total)})")
    log(f"  {len(manifest['volumes'])} volumes, {len(manifest['binds'])} bind mounts, {len(manifest['images'])} images, "
        f"{len(manifest['warnings'])} warnings, {len(manifest['errors'])} errors")
    for w in manifest["warnings"]:
        log(f"  warning: {w}")
    for e in manifest["errors"]:
        log(f"  ERROR: {e}")
    sys.exit(1 if manifest["errors"] else 0)


if __name__ == "__main__":
    main()
