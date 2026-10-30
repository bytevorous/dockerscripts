#!/usr/bin/env python3
"""
docker-volumes.py - every volume / bind mount "bridge" on a Docker host, with its from-to
locations.

Shows, per container, each mount as   host location  ->  container path  (rw/ro), then the
reverse view per named volume and per bind-mount host path: which containers use it, whether it
is shared by several containers, unused (no container mounts it), anonymous, or whether a bind
source is missing on this host. Also lists tmpfs mounts.
Read-only: docker ps / inspect / volume / system df calls (plus os.path.exists on bind sources).

  python3 docker-volumes.py
  python3 docker-volumes.py moodle              # only this container's mounts
  python3 docker-volumes.py --size              # volume sizes (docker system df -v, can be slow)
  python3 docker-volumes.py --json
  ssh user@server 'python3 - --no-check' < docker-volumes.py

--no-check skips the "does the bind source exist" test (use it when you are NOT on the docker host,
e.g. when reading a remote host through a docker context).
Options: --size  --no-check  --json  --context NAME  [container names ...]
"""
import argparse, json, os, re, shutil, subprocess, sys


def make_docker(ctx):
    base = ["docker"] + (["--context", ctx] if ctx else [])

    def docker(*args, check=True):
        r = subprocess.run(base + list(args), capture_output=True, text=True)
        if r.returncode != 0:
            if check:
                sys.exit("ERROR: " + (r.stderr.strip() or "docker " + " ".join(args)))
            return None
        return r.stdout
    return docker


def inspect(docker, kind, ids):
    return json.loads(docker(kind, "inspect", *ids)) if ids else []


def volume_sizes(docker):
    out = docker("system", "df", "-v", check=False) or ""
    sizes, inside = {}, False
    for ln in out.splitlines():
        if ln.startswith("Local Volumes space usage"):
            inside = True
            continue
        if not inside:
            continue
        if not ln.strip():
            if sizes:
                break
            continue
        if ln.startswith("VOLUME NAME"):
            continue
        p = ln.split()
        if len(p) >= 3:
            sizes[p[0]] = p[-1]
    return sizes


def main():
    ap = argparse.ArgumentParser(description="Volume and bind-mount map of Docker containers")
    ap.add_argument("names", nargs="*")
    ap.add_argument("--context")
    ap.add_argument("--size", action="store_true")
    ap.add_argument("--no-check", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not shutil.which("docker"):
        sys.exit("ERROR: docker CLI not found")
    docker = make_docker(a.context)

    all_containers = inspect(docker, "container", docker("ps", "-aq").split())
    volumes = {v["Name"]: v for v in inspect(docker, "volume", docker("volume", "ls", "-q").split())}
    sizes = volume_sizes(docker) if a.size else {}

    # usage across ALL containers (so "unused" is accurate even when filtering by name)
    used_names = {m["Name"] for c in all_containers for m in c.get("Mounts") or [] if m.get("Type") == "volume"}
    containers = all_containers
    if a.names:
        have = {c["Name"].lstrip("/") for c in all_containers}
        for n in a.names:
            if n not in have:
                print(f"warning: no container named '{n}'", file=sys.stderr)
        containers = [c for c in all_containers if c["Name"].lstrip("/") in a.names]

    rows = []
    for c in sorted(containers, key=lambda c: c["Name"]):
        n, state = c["Name"].lstrip("/"), c["State"].get("Status")
        seen_dest = set()
        for m in c.get("Mounts") or []:
            seen_dest.add(m["Destination"])
            rows.append({"container": n, "state": state, "type": m.get("Type"), "name": m.get("Name"),
                         "host": m.get("Source"), "container_path": m["Destination"], "rw": bool(m.get("RW", True)),
                         "driver": m.get("Driver")})
        for dst, opts in (c["HostConfig"].get("Tmpfs") or {}).items():
            if dst not in seen_dest:
                rows.append({"container": n, "state": state, "type": "tmpfs", "name": None, "host": None,
                             "container_path": dst, "rw": True, "driver": None, "opts": opts})

    vol_use, bind_use = {}, {}
    for r in rows:
        if r["type"] == "volume":
            vol_use.setdefault(r["name"], []).append(r)
        elif r["type"] == "bind":
            bind_use.setdefault(r["host"], []).append(r)

    vol_report = []
    for name in sorted(set(vol_use) | (set(volumes) if not a.names else set())):
        v = volumes.get(name, {})
        users = vol_use.get(name, [])
        vol_report.append({"name": name, "driver": v.get("Driver") or (users[0]["driver"] if users else None),
                           "host_location": v.get("Mountpoint") or (users[0]["host"] if users else None),
                           "size": sizes.get(name), "anonymous": bool(re.fullmatch(r"[0-9a-f]{64}", name)),
                           "unused": name not in used_names, "shared": len({u["container"] for u in users}) > 1,
                           "used_by": [{"container": u["container"], "path": u["container_path"], "rw": u["rw"]}
                                       for u in users]})
    bind_report = []
    for src, users in sorted(bind_use.items()):
        bind_report.append({"host_path": src, "exists_on_this_host": None if a.no_check else os.path.exists(src),
                            "shared": len({u["container"] for u in users}) > 1,
                            "used_by": [{"container": u["container"], "path": u["container_path"], "rw": u["rw"]}
                                        for u in users]})

    if a.json:
        print(json.dumps({"mounts": rows, "volumes": vol_report, "binds": bind_report}, indent=2))
        return
    if not rows and not vol_report:
        print("No mounts or volumes found.")
        return

    mode = lambda rw: "rw" if rw else "ro"
    print("BY CONTAINER   (host location  ->  container path)\n")
    for c in sorted({r["container"] for r in rows}):
        cr = [r for r in rows if r["container"] == c]
        print(f"{c}  ({cr[0]['state']})")
        for r in cr:
            if r["type"] == "tmpfs":
                print(f"    tmpfs    (memory)  ->  {r['container_path']}")
            elif r["type"] == "volume":
                print(f"    volume   {r['host']}  [{r['name']}]  ->  {r['container_path']}  ({mode(r['rw'])})")
            else:
                print(f"    bind     {r['host']}  ->  {r['container_path']}  ({mode(r['rw'])})")
        print()
    if not rows:
        print("(selected containers have no mounts)\n")

    print("NAMED VOLUMES   (volume  ->  host location, and who uses it)\n")
    for v in vol_report:
        flags = [f for f, on in (("SHARED", v["shared"]), ("UNUSED - no container mounts it", v["unused"]),
                                 ("anonymous", v["anonymous"])) if on]
        label = v["name"][:12] + "..." if v["anonymous"] else v["name"]
        print(f"{label}   driver {v['driver']}" + (f"   size {v['size']}" if v["size"] else "")
              + (f"   [{', '.join(flags)}]" if flags else ""))
        print(f"    host location: {v['host_location'] or '-'}")
        for u in v["used_by"]:
            print(f"    <- {u['container']}:{u['path']} ({mode(u['rw'])})")
        print()
    if not vol_report:
        print("(none)\n")

    print("BIND MOUNTS   (host path  ->  containers)\n")
    for b in bind_report:
        flags = []
        if b["exists_on_this_host"] is False:
            flags.append("MISSING on this host")
        if b["shared"]:
            flags.append("SHARED")
        print(b["host_path"] + (f"   [{', '.join(flags)}]" if flags else ""))
        for u in b["used_by"]:
            print(f"    -> {u['container']}:{u['path']} ({mode(u['rw'])})")
    if not bind_report:
        print("(none)")


if __name__ == "__main__":
    main()
