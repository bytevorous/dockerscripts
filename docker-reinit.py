#!/usr/bin/env python3
"""
docker-reinit.py - write one shell file PER CONTAINER containing the exact
`docker run -d` command that recreates it as it is now, in the form:

    docker stop NAME && docker rm NAME

    docker run -d \\
      --name NAME \\
      --restart unless-stopped \\
      -p 127.0.0.1:6806:6806 \\
      -v /host/path:/container/path \\
      -e TZ=Europe/London \\
      image \\
      command args

Only what differs from the image defaults is written. Containers on extra networks get
`docker network connect` lines after the run. Containers that were stopped get
`docker create` (made, not started). Missing user-defined networks are created first.
Reading the host is read-only (docker ps / inspect / ls); the generated files are only
executed when YOU run them - and they stop + remove the existing container of that name.

  python3 docker-reinit.py                         # current docker context
  python3 docker-reinit.py --context srv1 -o out
  python3 docker-reinit.py --stdout                # print instead of writing files
  ssh user@server 'python3 - -o ~/cc' < docker-reinit.py   # run on the server

Options: -o DIR  --stdout  --running-only  --keep-labels  --context NAME
"""
import argparse, datetime, json, os, re, shlex, shutil, subprocess, sys

q = shlex.quote
DEFAULT_SHM = 64 * 1024 * 1024
SKIP_NETS = {"bridge", "host", "none", "docker_gwbridge"}


def make_docker(ctx):
    base = ["docker"] + (["--context", ctx] if ctx else [])

    def docker(*args, check=True):
        r = subprocess.run(base + list(args), capture_output=True, text=True)
        if r.returncode != 0 and check:
            sys.exit("ERROR: " + (r.stderr.strip() or "docker " + " ".join(args)))
        return r.stdout if r.returncode == 0 else None
    return docker


def inspect(docker, kind, ids):
    return json.loads(docker(kind, "inspect", *ids)) if ids else []


def norm(x):
    return x or []


def fmt(args):
    """argv -> quoted shell command, continued onto new lines before flags."""
    lines, cur = [], ""
    for w in map(q, args):
        if cur and w.startswith("-") and len(cur) > 70:
            lines.append(cur)
            cur = "  " + w
        else:
            cur = f"{cur} {w}" if cur else w
    lines.append(cur)
    return " \\\n".join(lines)


def build(c, ref, icfg, cname, keep_labels):
    """-> (option groups, [network connect argv], dependency names, [image, *command])"""
    cfg, hc = c["Config"], c["HostConfig"]
    name = c["Name"].lstrip("/")
    groups, deps = [["--name", name]], set()
    add = groups.append
    nm = hc.get("NetworkMode") or "default"
    shares = nm in ("host", "none") or nm.startswith("container:")

    if cfg.get("Hostname") and not shares and cfg["Hostname"] != c["Id"][:12]:
        add(["--hostname", cfg["Hostname"]])
    if cfg.get("Domainname"):
        add(["--domainname", cfg["Domainname"]])
    for flag, k in (("--user", "User"), ("--workdir", "WorkingDir"), ("--stop-signal", "StopSignal")):
        if cfg.get(k) and cfg[k] != icfg.get(k):
            add([flag, cfg[k]])
    ienv = set(norm(icfg.get("Env")))
    for e in norm(cfg.get("Env")):
        if e not in ienv:
            add(["-e", e])
    ilab = icfg.get("Labels") or {}
    for k, v in sorted((cfg.get("Labels") or {}).items()):
        if ilab.get(k) != v and (keep_labels or not k.startswith("com.docker.compose.")):
            add(["--label", f"{k}={v}"])
    if cfg.get("Tty"):
        add(["-t"])
    if cfg.get("OpenStdin"):
        add(["-i"])
    if cfg.get("StopTimeout") is not None:
        add(["--stop-timeout", str(cfg["StopTimeout"])])
    for p in sorted(cfg.get("ExposedPorts") or {}):
        if p not in (icfg.get("ExposedPorts") or {}):
            add(["--expose", p])

    hk = cfg.get("Healthcheck")
    if hk and hk != icfg.get("Healthcheck"):
        t = norm(hk.get("Test"))
        if t[:1] == ["NONE"]:
            add(["--no-healthcheck"])
        elif t:
            add(["--health-cmd", t[1] if t[0] == "CMD-SHELL" else " ".join(map(q, t[1:]))])
            for k, flag in (("Interval", "--health-interval"), ("Timeout", "--health-timeout"),
                            ("StartPeriod", "--health-start-period")):
                if hk.get(k):
                    add([flag, f"{hk[k] // 1_000_000}ms"])
            if hk.get("Retries"):
                add(["--health-retries", str(hk["Retries"])])

    rp = hc.get("RestartPolicy") or {}
    if rp.get("Name") not in (None, "", "no"):
        add(["--restart", rp["Name"] + (f":{rp['MaximumRetryCount']}" if rp["Name"] == "on-failure"
                                        and rp.get("MaximumRetryCount") else "")])
    if hc.get("Privileged"):
        add(["--privileged"])
    if hc.get("ReadonlyRootfs"):
        add(["--read-only"])
    if hc.get("Init"):
        add(["--init"])
    for flag, k in (("--cap-add", "CapAdd"), ("--cap-drop", "CapDrop"), ("--security-opt", "SecurityOpt"),
                    ("--group-add", "GroupAdd"), ("--dns", "Dns"), ("--dns-search", "DnsSearch"),
                    ("--dns-option", "DnsOptions"), ("--add-host", "ExtraHosts")):
        for x in norm(hc.get(k)):
            add([flag, x])
    for k, v in sorted((hc.get("Sysctls") or {}).items()):
        add(["--sysctl", f"{k}={v}"])
    for u in norm(hc.get("Ulimits")):
        add(["--ulimit", f"{u['Name']}={u['Soft']}:{u['Hard']}"])
    for d in norm(hc.get("Devices")):
        add(["--device", f"{d['PathOnHost']}:{d['PathInContainer']}:{d['CgroupPermissions']}"])
    for r in norm(hc.get("DeviceRequests")):
        if any("gpu" in caps for caps in norm(r.get("Capabilities"))):
            add(["--gpus", ("device=" + ",".join(r["DeviceIDs"])) if r.get("DeviceIDs")
                 else ("all" if r.get("Count", -1) == -1 else str(r["Count"]))])
    for flag, k in (("--memory", "Memory"), ("--memory-reservation", "MemoryReservation")):
        if hc.get(k):
            add([flag, f"{hc[k]}b"])
    if (hc.get("MemorySwap") or 0) > 0:
        add(["--memory-swap", f"{hc['MemorySwap']}b"])
    if hc.get("NanoCpus"):
        add(["--cpus", f"{hc['NanoCpus'] / 1e9:g}"])
    if hc.get("CpuShares"):
        add(["--cpu-shares", str(hc["CpuShares"])])
    if hc.get("CpusetCpus"):
        add(["--cpuset-cpus", hc["CpusetCpus"]])
    if (hc.get("PidsLimit") or 0) > 0:
        add(["--pids-limit", str(hc["PidsLimit"])])
    if hc.get("ShmSize") and hc["ShmSize"] != DEFAULT_SHM:
        add(["--shm-size", f"{hc['ShmSize']}b"])
    if hc.get("Runtime") not in (None, "", "runc"):
        add(["--runtime", hc["Runtime"]])
    for flag, k, dflt in (("--pid", "PidMode", ("",)), ("--ipc", "IpcMode", ("", "private", "shareable")),
                          ("--uts", "UTSMode", ("",)), ("--userns", "UsernsMode", ("",))):
        v = hc.get(k) or ""
        if v not in dflt:
            if v.startswith("container:"):
                deps.add(cname(v.split(":", 1)[1]))
                v = "container:" + cname(v.split(":", 1)[1])
            add([flag, v])
    log = hc.get("LogConfig") or {}
    if log.get("Type") and (log["Type"] != "json-file" or log.get("Config")):
        add(["--log-driver", log["Type"]])
        for k, v in sorted((log.get("Config") or {}).items()):
            add(["--log-opt", f"{k}={v}"])

    if hc.get("PublishAllPorts"):
        add(["-P"])
    for cport, binds in sorted((hc.get("PortBindings") or {}).items()):
        for b in binds or [{}]:
            hip, hp = b.get("HostIp") or "", b.get("HostPort") or ""
            hip = f"[{hip}]" if ":" in hip else hip
            cport = cport[:-4] if cport.endswith("/tcp") else cport
            add(["-p", f"{hip}:{hp}:{cport}" if hip else (f"{hp}:{cport}" if hp else cport)])

    tmp = hc.get("Tmpfs") or {}
    for dst, o in sorted(tmp.items()):
        add(["--tmpfs", f"{dst}:{o}" if o else dst])
    for m in norm(c.get("Mounts")):
        t, dst = m.get("Type"), m["Destination"]
        o = [] if m.get("RW", True) else ["ro"]
        if m.get("Mode") in ("z", "Z"):
            o.append(m["Mode"])
        if t == "volume":
            src = m["Name"]
        elif t == "bind":
            src = m["Source"]
            if m.get("Propagation") and m["Propagation"] != "rprivate":
                o.append(m["Propagation"])
        elif t == "tmpfs":
            if dst not in tmp:
                add(["--tmpfs", dst])
            continue
        else:
            continue
        add(["-v", f"{src}:{dst}" + (":" + ",".join(o) if o else "")])

    connect = []
    if shares:
        if nm.startswith("container:"):
            deps.add(cname(nm.split(":", 1)[1]))
            add(["--network", "container:" + cname(nm.split(":", 1)[1])])
        else:
            add(["--network", nm])
    else:
        nets = c["NetworkSettings"].get("Networks") or {}
        primary = nm if nm in nets else ("bridge" if "bridge" in nets else next(iter(nets), None))
        legacy = []
        for l in norm(hc.get("Links")):
            t, al = l.split(":", 1)
            deps.add(cname(t))
            legacy.append(f"{cname(t)}:{al.rsplit('/', 1)[-1]}")
        for i, net in enumerate(([primary] + [n for n in nets if n != primary]) if primary else []):
            e = nets[net] or {}
            ipam = e.get("IPAMConfig") or {}
            f = []
            if net != "bridge":
                for al in norm(e.get("Aliases")):
                    if al not in (c["Id"][:12], name):
                        f += ["--network-alias" if i == 0 else "--alias", al]
            if ipam.get("IPv4Address"):
                f += ["--ip", ipam["IPv4Address"]]
            if ipam.get("IPv6Address"):
                f += ["--ip6", ipam["IPv6Address"]]
            for l in sorted(set((legacy if i == 0 else []) + norm(e.get("Links")))):
                f += ["--link", l]
                deps.add(l.split(":")[0])
            if i == 0:
                add(["--network", net, *f])
            else:
                connect.append(["docker", "network", "connect", *f, net, name])
        if cfg.get("MacAddress"):
            add(["--mac-address", cfg["MacAddress"]])

    ep, cm = norm(cfg.get("Entrypoint")), norm(cfg.get("Cmd"))
    tail = []
    if ep != norm(icfg.get("Entrypoint")):
        add(["--entrypoint", ep[0] if ep else ""])
        tail = ep[1:] + cm
    elif cm != norm(icfg.get("Cmd")):
        tail = cm
    return groups, connect, deps, [ref, *tail]


def render(verb, groups, image_tail):
    """Docker command with one option per line, ending in image and command."""
    parts = [verb]
    for g in groups:
        cur = []
        for t in g:
            if t.startswith("-") and cur:
                parts.append(" ".join(map(q, cur)))
                cur = []
            cur.append(t)
        parts.append(" ".join(map(q, cur)))
    parts.append(q(image_tail[0]))
    if image_tail[1:]:
        parts.append(" ".join(map(q, image_tail[1:])))
    return " \\\n  ".join(parts)


def net_create(n):
    a = ["docker", "network", "create", "--driver", n.get("Driver") or "bridge"]
    for flag, k in (("--internal", "Internal"), ("--attachable", "Attachable"), ("--ipv6", "EnableIPv6")):
        if n.get(k):
            a.append(flag)
    ipam = n.get("IPAM") or {}
    if ipam.get("Driver") not in (None, "", "default"):
        a += ["--ipam-driver", ipam["Driver"]]
    for x in norm(ipam.get("Config")):
        for k, flag in (("Subnet", "--subnet"), ("IPRange", "--ip-range"), ("Gateway", "--gateway")):
            if x.get(k):
                a += [flag, x[k]]
    for k, v in sorted((n.get("Options") or {}).items()):
        a += ["-o", f"{k}={v}"]
    for k, v in sorted((n.get("Labels") or {}).items()):
        if not k.startswith("com.docker.compose."):
            a += ["--label", f"{k}={v}"]
    return a + [n["Name"]]


def vol_create(v):
    a = ["docker", "volume", "create"]
    if v.get("Driver") and v["Driver"] != "local":
        a += ["--driver", v["Driver"]]
    for k, val in sorted((v.get("Options") or {}).items()):
        a += ["-o", f"{k}={val}"]
    for k, val in sorted((v.get("Labels") or {}).items()):
        if not k.startswith("com.docker.compose."):
            a += ["--label", f"{k}={val}"]
    return a + [v["Name"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context")
    ap.add_argument("-o", "--output", help="output folder (default ./container-commands-<host>-<time>)")
    ap.add_argument("--stdout", action="store_true", help="print all files to the terminal instead of writing them")
    ap.add_argument("--running-only", action="store_true")
    ap.add_argument("--keep-labels", action="store_true")
    a = ap.parse_args()
    if not shutil.which("docker"):
        sys.exit("ERROR: docker CLI not found")
    docker = make_docker(a.context)

    containers = inspect(docker, "container", docker("ps", "-q" if a.running_only else "-aq").split())
    if not containers:
        sys.exit("No containers found.")
    images = {i["Id"]: i for i in inspect(docker, "image", sorted({c["Image"] for c in containers}))}
    nets = {n["Name"]: n for n in inspect(docker, "network", docker("network", "ls", "-q").split())}
    vols = {v["Name"]: v for v in inspect(docker, "volume", docker("volume", "ls", "-q").split())}
    name_of = {c["Id"]: c["Name"].lstrip("/") for c in containers}

    def cname(ref):
        ref = ref.lstrip("/")
        for cid, n in name_of.items():
            if n == ref or (ref and cid.startswith(ref)):
                return n
        return ref

    used_n, used_v, built = set(), set(), {}
    for c in containers:
        n = name_of[c["Id"]]
        ref = c["Config"].get("Image") or c["Image"]
        got = docker("image", "inspect", "-f", "{{.Id}}", ref, check=False)
        if got is None or got.strip() != c["Image"]:
            print(f"# WARNING: {n}: tag '{ref}' no longer matches the image it runs; using image ID", file=sys.stderr)
            ref = c["Image"]
        icfg = (images.get(c["Image"]) or {}).get("Config") or {}
        built[n] = (c,) + build(c, ref, icfg, cname, a.keep_labels)
        used_n.update(k for k in (c["NetworkSettings"].get("Networks") or {}) if k not in SKIP_NETS)
        used_v.update(m["Name"] for m in norm(c.get("Mounts")) if m.get("Type") == "volume")

    # implicit dependencies: a container whose env values mention another container's name or
    # network alias, on a network they share (e.g. MOODLE_DATABASE_HOST=mariadb)
    inferred = {}
    idents = {}
    for m, (mc, *_r) in built.items():
        al = {m}
        for netinfo in (mc["NetworkSettings"].get("Networks") or {}).values():
            al.update(x for x in norm((netinfo or {}).get("Aliases")) if x != mc["Id"][:12])
        idents[m] = al
    for n, (c, _g, _cn, deps, _it) in built.items():
        words = {w for e in norm(c["Config"].get("Env"))
                 for w in re.split(r"[^A-Za-z0-9_.-]+", e.partition("=")[2]) if w}
        mine = set(c["NetworkSettings"].get("Networks") or {}) - SKIP_NETS
        for m, al in idents.items():
            if m == n or m in deps:
                continue
            if mine & set(built[m][0]["NetworkSettings"].get("Networks") or {}) and al & words:
                deps.add(m)
                inferred.setdefault(n, set()).add(m)

    order, done = [], set()

    def visit(n, stack=()):
        if n in done or n in stack or n not in built:
            return
        for d in sorted(built[n][3]):
            visit(d, stack + (n,))
        done.add(n)
        order.append(n)
    for n in sorted(built):
        visit(n)

    host = (docker("info", "-f", "{{.Name}}", check=False) or "?").strip()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", a.context or host)
    outdir = os.path.abspath(os.path.expanduser(a.output or f"container-commands-{label}-{stamp}"))
    files = {}
    for n in order:
        c, groups, connect, deps, image_tail = built[n]
        running = c["State"].get("Running")
        cn = sorted(k for k in (c["NetworkSettings"].get("Networks") or {}) if k not in SKIP_NETS and k in nets)
        extra_vols = [vols[m["Name"]] for m in norm(c.get("Mounts"))
                      if m.get("Type") == "volume" and m["Name"] in vols
                      and (vols[m["Name"]].get("Driver") != "local" or vols[m["Name"]].get("Options"))]
        binds = sorted({m["Source"] for m in norm(c.get("Mounts")) if m.get("Type") == "bind"})
        needs = sorted(d for d in deps if d in built and d != n and d not in inferred.get(n, ()))
        guess = sorted(inferred.get(n, ()))
        L = [f"# {n} - {'running' if running else c['State'].get('Status')} at export, image {c['Config'].get('Image')}"]
        if needs:
            L.append("# start these first: " + ", ".join(needs))
        if guess:
            L.append("# probably needs (env mentions it, same network): " + ", ".join(guess))
        if binds:
            L.append("# bind-mount host paths must exist: " + ", ".join(binds))
        if not running:
            L.append("# was not running: created only, not started")
        L += ["", f"docker stop {q(n)} && docker rm {q(n)}", ""]
        for name in cn:
            L.append(f"docker network inspect {q(name)} >/dev/null 2>&1 || \\\n{fmt(net_create(nets[name]))}")
        for v in extra_vols:
            L.append(f"docker volume inspect {q(v['Name'])} >/dev/null 2>&1 || \\\n{fmt(vol_create(v))}")
        if cn or extra_vols:
            L.append("")
        L.append(render("docker run -d" if running else "docker create", groups, image_tail))
        L += [fmt(x) for x in connect]
        files[re.sub(r"[^A-Za-z0-9_.-]", "_", n) + ".sh"] = "\n".join(L) + "\n"

    nrun = sum(1 for n in built if built[n][0]["State"].get("Running"))
    print(f"Docker host '{host}': {len(built)} containers found ({nrun} running, {len(built) - nrun} not running)",
          file=sys.stderr)

    if a.stdout:
        for f, text in files.items():
            print(f"\n##### {f}\n{text}")
        return
    os.makedirs(outdir, exist_ok=True)
    for f, text in files.items():
        with open(os.path.join(outdir, f), "w") as fh:
            fh.write(text)
        os.chmod(os.path.join(outdir, f), 0o755)
    print(f"Wrote {len(files)} file(s) to {outdir} (dependency order):", file=sys.stderr)
    for n in order:
        print("  " + re.sub(r"[^A-Za-z0-9_.-]", "_", n) + ".sh", file=sys.stderr)


if __name__ == "__main__":
    main()
