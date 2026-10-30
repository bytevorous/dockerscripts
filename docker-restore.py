#!/usr/bin/env python3
"""
docker-restore.py BACKUP - restore a folder made by docker-backup.py onto a Docker host.

Steps, in order:
  1. verify     every file against SHA256SUMS (stops on a mismatch; --no-verify skips this)
  2. images     docker load the saved images that are not on the host (others are pulled on create)
  3. networks   create the user-defined networks that do not exist yet
  4. volumes    create each named volume and extract its tarball into it
  5. containers recreate each container from reinit/<n>.sh in manifest "start_order" (databases
                before apps); containers that were stopped at backup time are created, not started

Anything that already exists on the host is linked as is and NEVER overwritten, unless you name it:
  existing image / network    linked as is
  existing volume             linked as is   --overwrite-volume NAME      empty it, refill from backup
  existing container          linked as is   --overwrite-container NAME   stop + remove, recreate from backup
  existing bind location      linked as is   (never written to, see below)
Both flags are repeatable, and named overwrites are always restored (also when not in --only).
Running containers that use a volume being overwritten are stopped while it is restored and started
again afterwards (unless they are themselves being overwritten, then they are recreated).
Everything linked is shown per item and listed under "Existing on the host" at the end.

Items that are not in the backup (or whose backup failed) are linked to what is on the host now:
an existing volume / network / container is used as is and never emptied or removed - also when it
was named with --overwrite-*. These are listed under "Not in the backup" at the end.

Bind mounts are NEVER written to: the restore does not extract binds/ archives and only checks that
each bind source path exists (read-only). Existing host locations are linked as they are and listed
per container. A container whose bind source paths are missing is skipped, because Docker would
silently create empty root-owned folders in their place; --allow-missing-binds creates it anyway.

  python3 docker-restore.py /mnt/backups/docker-backup-srv1-20261030-120000 --dry-run
  python3 docker-restore.py BACKUP
  python3 docker-restore.py BACKUP --only mariadb --only moodle --no-start
  python3 docker-restore.py BACKUP --overwrite-volume moodle_data --overwrite-container moodle
  ssh user@server 'python3 - /mnt/backups/docker-backup-...' < docker-restore.py

Run as root on the docker host (backup files are 0600). If the backup has no reinit/ folder (made
through ssh), docker-reinit.py must sit next to this script to rebuild the docker run commands.
Options: --dry-run  --only NAME  --overwrite-container NAME  --overwrite-volume NAME  --no-start
         --no-verify  --allow-missing-binds  --helper IMAGE  --context NAME
"""
import argparse, contextlib, functools, hashlib, importlib.util, json, os, re, shutil, subprocess, sys

BUILTIN_NETS = {"bridge", "host", "none", "docker_gwbridge"}
STOP_RM = re.compile(r"^docker stop .+ && docker rm .+$")


def make_docker(ctx):
    base = ["docker"] + (["--context", ctx] if ctx else [])

    def docker(*args, check=True):
        r = subprocess.run(base + list(args), capture_output=True, text=True)
        docker.err = r.stderr.strip()
        if r.returncode != 0:
            if check:
                sys.exit("ERROR: " + (docker.err or "docker " + " ".join(args)))
            return None
        return r.stdout
    docker.base, docker.err = base, ""
    return docker


def safe(s):
    """same file naming as docker-backup.py"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s.strip("/").replace("/", "__")) or "root"


def script_name(n):
    """same file naming as docker-reinit.py"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", n) + ".sh"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path):
    with open(path) as f:
        return json.load(f)


def hb(n):
    if n is None:
        return "unknown"
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def last_line(s):
    lines = [ln for ln in (s or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "unknown error"


def gnu_tar():
    try:
        return "GNU tar" in subprocess.run(["tar", "--version"], capture_output=True, text=True).stdout
    except OSError:
        return False


def verify(bdir):
    """-> (missing files, corrupt files). Missing items are linked to the host later; corrupt ones stop."""
    path = os.path.join(bdir, "SHA256SUMS")
    if not os.path.isfile(path):
        return [], ["SHA256SUMS not found"]
    missing, bad = [], []
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            digest, _, rel = line.partition("  ")
            full = os.path.join(bdir, rel)
            if not os.path.isfile(full):
                missing.append(rel)
            elif sha256(full) != digest:
                bad.append(f"checksum mismatch: {rel}")
    return missing, bad


def net_create_args(n):
    """docker network create from a `docker network inspect` entry (labels kept, so compose still owns it)"""
    a = ["network", "create", "--driver", n.get("Driver") or "bridge"]
    for flag, k in (("--internal", "Internal"), ("--attachable", "Attachable"), ("--ipv6", "EnableIPv6")):
        if n.get(k):
            a.append(flag)
    ipam = n.get("IPAM") or {}
    if ipam.get("Driver") not in (None, "", "default"):
        a += ["--ipam-driver", ipam["Driver"]]
    for x in ipam.get("Config") or []:
        for k, flag in (("Subnet", "--subnet"), ("IPRange", "--ip-range"), ("Gateway", "--gateway")):
            if x.get(k):
                a += [flag, x[k]]
    for k, v in sorted((n.get("Options") or {}).items()):
        a += ["-o", f"{k}={v}"]
    for k, v in sorted((n.get("Labels") or {}).items()):
        a += ["--label", f"{k}={v}"]
    return a + [n["Name"]]


def vol_create_args(v):
    """docker volume create from a `docker volume inspect` entry (labels kept, so compose still owns it)"""
    a = ["volume", "create"]
    if v.get("Driver") and v["Driver"] != "local":
        a += ["--driver", v["Driver"]]
    for k, val in sorted((v.get("Options") or {}).items()):
        a += ["-o", f"{k}={val}"]
    for k, val in sorted((v.get("Labels") or {}).items()):
        a += ["--label", f"{k}={val}"]
    return a + [v["Name"]]


def host_dir(docker, name, direct):
    """The volume's folder on this machine, when it can be used directly (local driver, no options)."""
    if not direct:
        return None
    out = docker("volume", "inspect", name, check=False)
    info = json.loads(out)[0] if out else {}
    if info.get("Driver") != "local" or info.get("Options"):
        return None
    mp = info.get("Mountpoint")
    return mp if mp and os.path.isdir(mp) else None


def wipe_volume(docker, helper, name):
    if docker("run", "--rm", "-v", f"{name}:/dst", helper, "sh", "-c",
              "rm -rf /dst/..?* /dst/.[!.]* /dst/*", check=False) is None:
        raise RuntimeError("could not empty the volume: " + last_line(docker.err))


def fill_volume(docker, helper, name, tarball, direct):
    """Extract tarball into the volume: host GNU tar as root (exact numeric owners), else the helper."""
    d = host_dir(docker, name, direct)
    if d and os.geteuid() == 0 and gnu_tar():
        r = subprocess.run(["tar", "--numeric-owner", "-xzpf", tarball, "-C", d], capture_output=True, text=True)
        if r.returncode == 0:
            return "host tar"
    with open(tarball, "rb") as f:                   # streamed, so it also works with a remote --context
        r = subprocess.run(docker.base + ["run", "--rm", "-i", "-v", f"{name}:/dst", helper,
                                          "tar", "xzpf", "-", "-C", "/dst"], stdin=f, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("extract failed: " + last_line(r.stderr.decode(errors="replace")))
    return f"helper {helper}"


def running_users(docker, vol):
    out = docker("ps", "--filter", f"volume={vol}", "--format", "{{.Names}}", check=False) or ""
    return out.split()


@contextlib.contextmanager
def stopped(docker, names, keep_stopped, log):
    """Stop names for the duration; start them again afterwards unless they are in keep_stopped."""
    acted = []
    try:
        for n in names:
            log(f"    stop {n}")
            if docker("stop", n, check=False) is None:
                raise RuntimeError(f"could not stop {n}: {last_line(docker.err)}")
            acted.append(n)
        yield
    finally:
        for n in reversed(acted):
            if n in keep_stopped:
                continue
            log(f"    start {n}")
            if docker("start", n, check=False) is None:
                log(f"    WARNING: could not start {n} - do it manually!")


def make_path_check(docker, helper, direct):
    def check(p):
        if direct:
            return os.path.exists(p)
        return docker("run", "--rm", "-v", "/:/host:ro", helper, "test", "-e", "/host" + p, check=False) is not None
    return check


@functools.lru_cache(maxsize=None)
def reinit_module():
    here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ""
    path = os.path.join(here, "docker-reinit.py") if here else ""
    if not path or not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("docker_reinit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def container_script(bdir, n, c, docker, name_of):
    """-> (shell text that creates the container, where it came from). Never contains stop/rm."""
    path = os.path.join(bdir, "reinit", script_name(n))
    if os.path.isfile(path):
        with open(path) as f:
            text = "\n".join(ln for ln in f.read().splitlines() if not STOP_RM.match(ln))
        return text, "reinit/" + script_name(n)
    mod = reinit_module()
    if mod is None:
        raise RuntimeError("no reinit/ script in the backup and docker-reinit.py is not next to this script")
    ref = c["Config"].get("Image") or c["Image"]
    out = docker("image", "inspect", ref, check=False)
    icfg = (json.loads(out)[0].get("Config") or {}) if out else {}

    def cname(r):
        r = r.lstrip("/")
        for cid, nm in name_of.items():
            if nm == r or (r and cid.startswith(r)):
                return nm
        return r
    groups, connect, _deps, tail = mod.build(c, ref, icfg, cname, False)
    verb = "docker run -d" if c["State"].get("Running") else "docker create"
    return "\n".join([mod.render(verb, groups, tail)] + [mod.fmt(x) for x in connect]), "rebuilt from inspect.json"


def main():
    ap = argparse.ArgumentParser(description="Restore a docker-backup.py backup onto a Docker host")
    ap.add_argument("backup", help="the docker-backup-<host>-<time> folder")
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="restore only this container and its volumes (repeatable)")
    ap.add_argument("--overwrite-container", action="append", default=[], metavar="NAME",
                    help="stop + remove this existing container and recreate it from the backup (repeatable)")
    ap.add_argument("--overwrite-volume", action="append", default=[], metavar="NAME",
                    help="empty this existing volume and refill it from the backup (repeatable)")
    ap.add_argument("--no-start", action="store_true", help="create containers but do not start them")
    ap.add_argument("--no-verify", action="store_true", help="skip the SHA256SUMS check")
    ap.add_argument("--allow-missing-binds", action="store_true",
                    help="create containers even when bind source paths are missing on the host")
    ap.add_argument("--helper", default="alpine", help="image used to read/write volumes (default alpine)")
    ap.add_argument("--context")
    ap.add_argument("--dry-run", action="store_true", help="show what would be restored; change nothing")
    a = ap.parse_args()
    if not shutil.which("docker"):
        sys.exit("ERROR: docker CLI not found")
    docker = make_docker(a.context)
    direct = not a.context                          # host paths only mean something when docker is local
    bdir = os.path.abspath(os.path.expanduser(a.backup))
    mpath = os.path.join(bdir, "manifest.json")
    if not os.path.isfile(mpath):
        sys.exit(f"ERROR: {mpath} not found - point at a docker-backup-<host>-<time> folder")
    manifest = load_json(mpath)
    log = lambda s: print(s, flush=True)
    errors, notes, done, linked = [], [], [], []
    existing = {k: [] for k in ("image", "network", "volume", "container", "bind")}

    backup, no_inspect = {}, []
    for item in manifest.get("containers", []):
        p = os.path.join(bdir, "containers", safe(item["name"]), "inspect.json")
        if not os.path.isfile(p):
            no_inspect.append(item["name"])           # treated as not backed up: linked to the host
            continue
        backup[item["name"]] = load_json(p)
    vol_items = {v["name"]: v for v in manifest.get("volumes", [])}
    not_backed = [n for n in dict.fromkeys([x for x in no_inspect if not a.only or x in a.only
                                            or x in a.overwrite_container]
                                           + a.only + a.overwrite_container) if n not in backup]
    only = [n for n in a.only if n in backup]
    ow_c, ow_v = {n for n in a.overwrite_container if n in backup}, set(a.overwrite_volume)

    names = (set(only) if a.only else set(backup)) | ow_c
    so = manifest.get("start_order") or []
    order = [n for n in so if n in names] + sorted(n for n in names if n not in so)
    if not so:
        notes.append("backup has no start_order: containers were created alphabetically")
    vols = sorted({m["Name"] for n in order for m in backup[n].get("Mounts") or [] if m.get("Type") == "volume"}
                  | ow_v)
    nets = sorted({k for n in order for k in (backup[n]["NetworkSettings"].get("Networks") or {})
                   if k not in BUILTIN_NETS})

    host = (docker("info", "--format", "{{.Name}}", check=False) or "?").strip()
    log(f"Backup:  {bdir}")
    log(f"         made {manifest.get('created')} on host '{manifest.get('host')}'")
    log(f"Target:  docker host '{host}'" + (f" (context {a.context})" if a.context else ""))
    log(f"Scope:   {len(order)} containers, {len(vols)} volumes, {len(nets)} networks")
    if ow_c or ow_v:
        log("Overwrite: " + ", ".join([f"container {x}" for x in sorted(ow_c)] + [f"volume {x}" for x in sorted(ow_v)]))
    if a.dry_run:
        log("\nDRY RUN - nothing will be changed.")
    if not_backed:
        log("\nNamed but not in the backup:")
        for n in not_backed:
            if docker("container", "inspect", n, check=False) is not None:
                linked.append(f"container {n}: not in the backup - existing container left as is")
                log(f"  {n:<30} not in the backup - linked to the existing container (left as is)")
            else:
                linked.append(f"container {n}: not in the backup and not on the host - nothing to restore")
                log(f"  {n:<30} not in the backup and not on the host - nothing to restore")

    # 1. verify -------------------------------------------------------------------------------------
    if a.no_verify:
        notes.append("checksums were not verified (--no-verify)")
    else:
        log("\nVerifying SHA256SUMS ...")
        bad = verify(bdir)
        for b in bad:
            log(f"  {b}")
        if bad:
            sys.exit("ERROR: the backup failed verification - nothing restored (--no-verify to restore anyway)")
        log("  all files match")

    # 2. images -------------------------------------------------------------------------------------
    log("\nImages:")
    img_items = {i["name"]: i for i in manifest.get("images", []) if i.get("file") and not i.get("error")}
    seen = set()
    for n in order:
        c = backup[n]
        ref = c["Config"].get("Image") or c["Image"]
        if ref in seen:
            continue
        seen.add(ref)
        if docker("image", "inspect", ref, check=False) is not None:
            existing["image"].append(ref)
            log(f"  {ref:<50} exists - linked as is (never overwritten)")
            continue
        item = img_items.get(ref) or img_items.get(c["Image"])
        if not item:
            log(f"  {ref:<50} not on host, not in backup - pulled when the container is created")
            continue
        log(f"  {ref:<50} load {item['file']} ({hb(item.get('bytes'))})")
        if a.dry_run:
            continue
        if docker("load", "-i", os.path.join(bdir, item["file"]), check=False) is None:
            errors.append(f"image {ref}: docker load failed: {last_line(docker.err)}")
            log("    FAILED")
        else:
            done.append(f"image {ref}")

    # 3. networks -----------------------------------------------------------------------------------
    log(f"\nNetworks ({len(nets)}):")
    for nn in nets:
        if docker("network", "inspect", nn, check=False) is not None:
            existing["network"].append(nn)
            log(f"  {nn:<30} exists - linked as is (never overwritten)")
            continue
        p = os.path.join(bdir, "networks", safe(nn) + ".json")
        if not os.path.isfile(p):
            linked.append(f"network {nn}: not in the backup and not on the host - containers on it may fail")
            log(f"  {nn:<30} not in the backup and not on the host - nothing to link to")
            continue
        log(f"  {nn:<30} create")
        if a.dry_run:
            continue
        if docker(*net_create_args(load_json(p)[0]), check=False) is None:
            errors.append(f"network {nn}: {last_line(docker.err)}")
            log(f"    FAILED: {last_line(docker.err)}")
        else:
            done.append(f"network {nn}")

    # 4. volumes ------------------------------------------------------------------------------------
    log(f"\nVolumes ({len(vols)}):")
    for vn in vols:
        item = vol_items.get(vn)
        if not item or item.get("error") or not item.get("file"):
            why = "its backup failed" if item and item.get("error") else "not in the backup"
            if docker("volume", "inspect", vn, check=False) is not None:
                linked.append(f"volume {vn}: {why} - existing volume used as is"
                              + (" (--overwrite-volume ignored)" if vn in ow_v else ""))
                log(f"  {vn:<30} {why} - linked to the existing volume (left as is)")
            else:
                linked.append(f"volume {vn}: {why} and not on the host - Docker creates it empty with its container")
                log(f"  {vn:<30} {why} and not on the host - will be created empty")
            continue
        tarball = os.path.join(bdir, item["file"])
        if docker("volume", "inspect", vn, check=False) is None:
            action = "create"
        elif vn in ow_v:
            action = "overwrite"
        else:
            existing["volume"].append(vn)
            log(f"  {vn:<30} exists - linked as is, never overwritten (--overwrite-volume {vn} to replace)")
            continue
        users = running_users(docker, vn) if action == "overwrite" else []
        log(f"  {vn:<30} {action} from {item['file']} ({hb(item.get('bytes'))})"
            + (f"   stopped meanwhile: {', '.join(users)}" if users else ""))
        if a.dry_run:
            continue
        try:
            if action == "create":
                ip = os.path.join(bdir, "volumes", safe(vn) + ".inspect.json")
                spec = load_json(ip)[0] if os.path.isfile(ip) else {"Name": vn}
                if docker(*vol_create_args(spec), check=False) is None:
                    raise RuntimeError("docker volume create failed: " + last_line(docker.err))
            with stopped(docker, users, ow_c, log):
                if action == "overwrite":
                    wipe_volume(docker, a.helper, vn)
                method = fill_volume(docker, a.helper, vn, tarball, direct)
            done.append(f"volume {vn}")
            log(f"    ok ({method})")
        except (RuntimeError, OSError) as e:
            errors.append(f"volume {vn}: {e}")
            log(f"    FAILED: {e}")

    # 5. containers ---------------------------------------------------------------------------------
    log(f"\nContainers ({len(order)}):")
    name_of = {c["Id"]: n for n, c in backup.items()}
    env = dict(os.environ, **({"DOCKER_CONTEXT": a.context} if a.context else {}))
    path_ok = make_path_check(docker, a.helper, direct)
    for n in order:
        c = backup[n]
        present = docker("container", "inspect", n, check=False) is not None
        if present and n not in ow_c:
            existing["container"].append(n)
            log(f"  {n:<30} exists - linked as is, never overwritten (--overwrite-container {n} to replace)")
            continue
        binds = sorted({m["Source"] for m in c.get("Mounts") or [] if m.get("Type") == "bind"})
        missing = [b for b in binds if not path_ok(b)]
        if missing and not a.allow_missing_binds:
            errors.append(f"container {n}: bind source missing on the host: {', '.join(missing)}")
            log(f"  {n:<30} SKIPPED - bind source missing: {', '.join(missing)}")
            continue
        try:
            script, source = container_script(bdir, n, c, docker, name_of)
        except (RuntimeError, OSError, KeyError) as e:
            errors.append(f"container {n}: {e}")
            log(f"  {n:<30} FAILED: {e}")
            continue
        if a.no_start:
            script = re.sub(r"(?m)^docker run -d\b", "docker create", script, count=1)
        start = bool(c["State"].get("Running")) and not a.no_start
        log(f"  {n:<30} {'overwrite' if present else 'create'}, {'start' if start else 'not started'}  ({source})")
        for b in binds:
            if b not in missing:
                if b not in existing["bind"]:
                    existing["bind"].append(b)
                log(f"      bind {b}  linked to existing location (never overwritten)")
        if missing:
            notes.append(f"container {n}: created although bind sources are missing: {', '.join(missing)}")
        if a.dry_run:
            continue
        if present:
            docker("stop", n, check=False)
            if docker("rm", n, check=False) is None:
                errors.append(f"container {n}: could not remove the existing container: {last_line(docker.err)}")
                log(f"    FAILED: {last_line(docker.err)}")
                continue
        r = subprocess.run(["sh", "-c", "set -e\n" + script], capture_output=True, text=True, env=env)
        if r.returncode != 0:
            errors.append(f"container {n}: {last_line(r.stderr)}")
            log(f"    FAILED: {last_line(r.stderr)}")
        else:
            done.append(f"container {n}")
            log("    ok")

    nb = sum(1 for b in manifest.get("binds", []) if b.get("file") and not b.get("error"))
    if nb:
        notes.append(f"{nb} bind-mount archive(s) in the backup were not restored (see RESTORE.txt)")

    n_existing = sum(len(v) for v in existing.values())
    log("\n" + ("DRY RUN - nothing was changed." if a.dry_run else
                f"Done: {len(done)} restored, {n_existing} existing linked as is, {len(linked)} not in backup, "
                f"{len(errors)} errors"))
    if n_existing:
        log("\nExisting on the host - linked as is, never overwritten:")
        for kind in ("image", "network", "volume", "container", "bind"):
            if existing[kind]:
                log(f"  {kind + 's:':<12}" + ", ".join(existing[kind]))
        if existing["volume"] or existing["container"]:
            log("  (replace a volume or container with --overwrite-volume NAME / --overwrite-container NAME)")
    if linked:
        log("\nNot in the backup - linked to what is on the host (nothing deleted or emptied):")
        for x in linked:
            log(f"  {x}")
    for x in notes:
        log(f"  note: {x}")
    for e in errors:
        log(f"  ERROR: {e}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
