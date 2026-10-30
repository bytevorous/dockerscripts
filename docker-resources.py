#!/usr/bin/env python3
"""
docker-resources.py - resources consumed by each container on a Docker host.

Shows live CPU %, memory used / configured limit, network and disk I/O, process count and the
configured CPU / memory / pid limits, plus totals and the share of the host.
Read-only: only docker stats / inspect / info / ps calls.

  python3 docker-resources.py                    # running containers, sorted by memory
  python3 docker-resources.py --sort cpu         # cpu | mem | net | disk | name
  python3 docker-resources.py --all --size       # include stopped; add writable-layer size (slower)
  python3 docker-resources.py moodle mariadb     # only these
  python3 docker-resources.py --json
  ssh user@server 'python3 -' < docker-resources.py

Note: CPU % is per core, so a container using two full cores shows 200%.
Options: --sort KEY  --all  --size  --json  --context NAME  [container names ...]
"""
import argparse, json, re, shutil, subprocess, sys

UNITS = {"": 1, "b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
         "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


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


def parse_bytes(s):
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]*)\s*$", s or "")
    if not m:
        return 0
    return int(float(m.group(1)) * UNITS.get(m.group(2).lower(), 1))


def pct(s):
    try:
        return float((s or "").strip().rstrip("%"))
    except ValueError:
        return 0.0


def pair(s):
    a, _, b = (s or "").partition("/")
    return parse_bytes(a), parse_bytes(b)


def hb(n):
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def host_info(docker):
    out = docker("info", "--format", "{{.NCPU}}|{{.MemTotal}}", check=False) or ""
    try:
        n, m = out.strip().split("|")
        return int(n), int(m)
    except ValueError:
        return 0, 0


def limits(c):
    hc = c["HostConfig"]
    cpus = (hc.get("NanoCpus") or 0) / 1e9
    if not cpus and (hc.get("CpuQuota") or 0) > 0 and hc.get("CpuPeriod"):
        cpus = hc["CpuQuota"] / hc["CpuPeriod"]
    return {"mem": hc.get("Memory") or 0, "cpus": cpus, "pids": hc.get("PidsLimit") or 0}


def main():
    ap = argparse.ArgumentParser(description="Resources consumed by Docker containers")
    ap.add_argument("names", nargs="*")
    ap.add_argument("--context")
    ap.add_argument("--sort", choices=("cpu", "mem", "net", "disk", "name"), default="mem")
    ap.add_argument("--all", action="store_true", help="also list stopped containers")
    ap.add_argument("--size", action="store_true", help="include writable-layer size (docker ps -s, slower)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not shutil.which("docker"):
        sys.exit("ERROR: docker CLI not found")
    docker = make_docker(a.context)

    ids = docker("ps", "-aq").split()
    insp = {c["Name"].lstrip("/"): c for c in (json.loads(docker("container", "inspect", *ids)) if ids else [])}
    if a.names:
        for n in a.names:
            if n not in insp:
                print(f"warning: no container named '{n}'", file=sys.stderr)
        insp = {k: v for k, v in insp.items() if k in a.names}

    stats = {}
    for line in (docker("stats", "--no-stream", "--format", "{{json .}}", check=False) or "").splitlines():
        try:
            s = json.loads(line)
            stats[s["Name"]] = s
        except (ValueError, KeyError):
            pass

    sizes = {}
    if a.size:
        for line in (docker("ps", "-a", "-s", "--format", "{{.Names}}|{{.Size}}", check=False) or "").splitlines():
            n, _, sz = line.partition("|")
            sizes[n] = parse_bytes(sz.split(" (")[0])

    rows = []
    for name, c in insp.items():
        s = stats.get(name)
        lim = limits(c)
        if not s and not a.all:
            continue
        r = {"name": name, "state": c["State"].get("Status"), "running": bool(s),
             "cpu": pct(s["CPUPerc"]) if s else 0.0,
             "mem": pair(s["MemUsage"])[0] if s else 0,
             "mem_pct": pct(s["MemPerc"]) if s else 0.0,
             "net_rx": pair(s["NetIO"])[0] if s else 0, "net_tx": pair(s["NetIO"])[1] if s else 0,
             "blk_r": pair(s["BlockIO"])[0] if s else 0, "blk_w": pair(s["BlockIO"])[1] if s else 0,
             "pids": int(s["PIDs"]) if s and str(s.get("PIDs", "")).isdigit() else 0,
             "limit_mem": lim["mem"], "limit_cpus": lim["cpus"], "limit_pids": lim["pids"]}
        if a.size:
            r["rw_layer"] = sizes.get(name, 0)
        rows.append(r)

    key = {"cpu": lambda r: -r["cpu"], "mem": lambda r: -r["mem"], "net": lambda r: -(r["net_rx"] + r["net_tx"]),
           "disk": lambda r: -(r["blk_r"] + r["blk_w"]), "name": lambda r: r["name"]}[a.sort]
    rows.sort(key=key)
    ncpu, memtotal = host_info(docker)
    tot = {k: sum(r[k] for r in rows) for k in ("cpu", "mem", "net_rx", "net_tx", "blk_r", "blk_w", "pids")}

    if a.json:
        print(json.dumps({"host": {"cpus": ncpu, "mem_total": memtotal}, "totals": tot, "containers": rows}, indent=2))
        return
    if not rows:
        print("No running containers (use --all to list stopped ones).")
        return

    head = ["NAME", "CPU%", "MEM USED", "MEM%", "MEM LIMIT", "CPU LIMIT", "NET RX/TX", "DISK R/W", "PIDS"]
    if a.size:
        head.append("RW LAYER")
    body = []
    for r in rows:
        if not r["running"]:
            row = [r["name"], f"({r['state']})"] + ["-"] * (len(head) - 2)
            if a.size:
                row[-1] = hb(r["rw_layer"])
            body.append(row)
            continue
        row = [r["name"], f"{r['cpu']:.1f}", hb(r["mem"]), f"{r['mem_pct']:.1f}",
               hb(r["limit_mem"]) if r["limit_mem"] else "none",
               f"{r['limit_cpus']:g}" if r["limit_cpus"] else "none",
               f"{hb(r['net_rx'])}/{hb(r['net_tx'])}", f"{hb(r['blk_r'])}/{hb(r['blk_w'])}", str(r["pids"])]
        if a.size:
            row.append(hb(r["rw_layer"]))
        body.append(row)
    total = ["TOTAL", f"{tot['cpu']:.1f}", hb(tot["mem"]), "", "", "", f"{hb(tot['net_rx'])}/{hb(tot['net_tx'])}",
             f"{hb(tot['blk_r'])}/{hb(tot['blk_w'])}", str(tot["pids"])]
    if a.size:
        total.append(hb(sum(r.get("rw_layer", 0) for r in rows)))
    w = [max(len(x[i]) for x in [head] + body + [total]) for i in range(len(head))]
    line = "  ".join(("{:<%d}" if i == 0 else "{:>%d}") % n for i, n in enumerate(w))
    print(line.format(*head))
    for row in body:
        print(line.format(*row))
    print(line.format(*["-" * n for n in w]))
    print(line.format(*total))
    if ncpu and memtotal:
        print(f"\nHost: {ncpu} CPUs, {hb(memtotal)} RAM  ->  containers use {tot['cpu'] / (ncpu * 100) * 100:.1f}% of CPU "
              f"and {tot['mem'] / memtotal * 100:.1f}% of RAM")
    stopped = sum(1 for c in insp.values() if c["State"].get("Status") != "running")
    if stopped and not a.all:
        print(f"{stopped} stopped container(s) not shown (use --all).")


if __name__ == "__main__":
    main()
