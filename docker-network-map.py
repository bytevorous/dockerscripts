#!/usr/bin/env python3
"""
docker-network-map.py - every network port and link of every container on a Docker host.

For each container it shows:
  * published ports (host ip:port -> container port) and whether they are open to ALL interfaces,
    localhost only, or one address; plus exposed-but-unpublished ports
  * its networks: driver, subnet, its IP, gateway, aliases, the host bridge interface
  * internal links: --link entries, the other containers on its networks, and containers it
    references through environment variables (e.g. MOODLE_DATABASE_HOST=mariadb)
  * outside references found in environment variables (URLs / *_HOST / *_SERVER / *_ENDPOINT /
    *_ADDR values) - only the host names are printed, never passwords or other values
  * with --live: the sockets actually open inside the container right now (listening ports and
    established connections, classified as another container / docker host / LAN / EXTERNAL)

Read-only: docker ps / inspect / network calls; --live additionally reads /proc/<pid>/net/tcp*
on the docker host (Linux, needs root and must run ON the docker host).

  python3 docker-network-map.py
  python3 docker-network-map.py moodle              # only this container
  sudo python3 docker-network-map.py --live
  python3 docker-network-map.py --json
  ssh user@server 'python3 -' < docker-network-map.py

Options: --live  --json  --context NAME  [container names ...]
"""
import argparse, ipaddress, json, re, shutil, socket, subprocess, sys

URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/\s]*@)?(\[[0-9a-fA-F:]+\]|[^/:\s?#@]+)(?::(\d+))?")
HOSTKEY_RE = re.compile(r"(HOST|SERVER|ENDPOINT|ADDR)", re.I)
HOSTVAL_RE = re.compile(r"^[A-Za-z0-9_.-]+(?::\d+)?$")
NOISE_HOSTS = {"localhost", "0.0.0.0", "127.0.0.1", "::1"}


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


def env_refs(c):
    refs = []
    for e in c["Config"].get("Env") or []:
        k, _, v = e.partition("=")
        v = v.strip()
        if not v:
            continue
        m = URL_RE.search(v)
        if m:
            refs.append((k, m.group(1).strip("[]"), m.group(2)))
        elif HOSTKEY_RE.search(k) and HOSTVAL_RE.match(v) and not v.isdigit():
            h, _, p = v.partition(":")
            refs.append((k, h, p or None))
    return refs


def classify(host, me, idents, ip_owner, gateways):
    """-> (kind, container, network)  kind: self|internal|local|docker-host|lan|external"""
    h = host.lower()
    mine = idents.get(me, {})
    if any(h in {x.lower() for x in names} for names in mine.values()):
        return ("self", me, None)
    for other, d in idents.items():
        if other == me:
            continue
        for net, names in d.items():
            if net in mine and h in {x.lower() for x in names}:
                return ("internal", other, net)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if h == "localhost":
            return ("local", None, None)
        if h == "host.docker.internal":
            return ("docker-host", None, None)
        return ("external", None, None)
    s = str(ip)
    if s in ip_owner:
        return ("self" if ip_owner[s] == me else "internal", ip_owner[s], None)
    if ip.is_loopback or ip.is_unspecified:
        return ("local", None, None)
    if s in gateways:
        return ("docker-host", None, gateways[s])
    if ip.is_private or ip.is_link_local:
        return ("lan", None, None)
    return ("external", None, None)


def dec(addr):
    ip_hex, port_hex = addr.split(":")
    raw = bytes.fromhex(ip_hex)
    if len(raw) == 4:
        ip = socket.inet_ntoa(raw[::-1])
    else:
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        v6 = ipaddress.IPv6Address(raw)
        ip = str(v6.ipv4_mapped) if v6.ipv4_mapped else str(v6)
    return ip, int(port_hex, 16)


def live_sockets(pid):
    res = {"listen": set(), "established": [], "error": None}
    ok = False
    for path in (f"/proc/{pid}/net/tcp", f"/proc/{pid}/net/tcp6"):
        try:
            with open(path) as fh:
                lines = fh.read().splitlines()[1:]
        except OSError as e:
            res["error"] = f"cannot read {path}: {e.strerror} (run as root, on the docker host)"
            continue
        ok = True
        for ln in lines:
            f = ln.split()
            if len(f) < 4:
                continue
            lip, lport = dec(f[1])
            rip, rport = dec(f[2])
            if f[3] == "0A":
                res["listen"].add(lport)
            elif f[3] == "01":
                res["established"].append((rip, rport))
    if ok:
        res["error"] = None
    res["listen"] = sorted(res["listen"])
    return res


def scope_of(ip):
    if ip in ("", "0.0.0.0", "::"):
        return "ALL interfaces"
    if ip.startswith("127.") or ip == "::1":
        return "localhost only"
    return f"{ip} only"


def analyse(containers, nets, live):
    by_name = {c["Name"].lstrip("/"): c for c in containers}
    ip_owner, idents, gateways = {}, {}, {}
    for n, c in by_name.items():
        d = {}
        for net, e in (c["NetworkSettings"].get("Networks") or {}).items():
            e = e or {}
            for key in ("IPAddress", "GlobalIPv6Address"):
                if e.get(key):
                    ip_owner[e[key]] = n
            d[net] = {n} | {x for x in (e.get("Aliases") or []) if x != c["Id"][:12]}
        idents[n] = d
    for nn, net in nets.items():
        for cfg in (net.get("IPAM") or {}).get("Config") or []:
            if cfg.get("Gateway"):
                gateways[cfg["Gateway"]] = nn

    out = []
    for n, c in sorted(by_name.items()):
        hc, st = c["HostConfig"], c["State"]
        live_ports = c["NetworkSettings"].get("Ports") or {}
        cfg_ports = hc.get("PortBindings") or {}
        published, unpublished = [], []
        for cport in sorted(set((c["Config"].get("ExposedPorts") or {}).keys()) | set(live_ports) | set(cfg_ports)):
            binds = live_ports.get(cport) or cfg_ports.get(cport) or []
            port, _, proto = cport.partition("/")
            if not binds:
                unpublished.append(cport)
            for b in binds:
                ip = b.get("HostIp") or ""
                published.append({"host_ip": ip or "0.0.0.0", "host_port": b.get("HostPort") or "(random)",
                                  "container_port": port, "proto": proto or "tcp", "scope": scope_of(ip)})
        networks, neighbours = [], {}
        for net, e in (c["NetworkSettings"].get("Networks") or {}).items():
            e = e or {}
            ninfo = nets.get(net, {})
            subnets = [x.get("Subnet") for x in (ninfo.get("IPAM") or {}).get("Config") or [] if x.get("Subnet")]
            iface = (ninfo.get("Options") or {}).get("com.docker.network.bridge.name")
            if not iface and ninfo.get("Driver") == "bridge":
                iface = "docker0" if net == "bridge" else "br-" + (ninfo.get("Id") or "")[:12]
            networks.append({"network": net, "driver": ninfo.get("Driver"), "internal": bool(ninfo.get("Internal")),
                             "subnets": subnets, "ip": e.get("IPAddress") or "", "prefix": e.get("IPPrefixLen"),
                             "gateway": e.get("Gateway") or "", "bridge_iface": iface,
                             "aliases": [x for x in (e.get("Aliases") or []) if x != c["Id"][:12] and x != n]})
            neighbours[net] = [{"name": o, "ip": ((by_name[o]["NetworkSettings"]["Networks"].get(net) or {})
                                                  .get("IPAddress") or "")}
                               for o in sorted(by_name) if o != n
                               and net in (by_name[o]["NetworkSettings"].get("Networks") or {})]
        links = [l.replace("/", "") for l in (hc.get("Links") or [])]
        internal, external = [], []
        for var, host, port in env_refs(c):
            if host.lower() in NOISE_HOSTS:
                continue
            kind, other, net = classify(host, n, idents, ip_owner, gateways)
            if kind == "self":
                continue
            item = {"var": var, "host": host, "port": port, "kind": kind, "container": other, "network": net}
            (internal if kind == "internal" else external).append(item)
        r = {"name": n, "state": st.get("Status"), "image": c["Config"].get("Image"),
             "network_mode": hc.get("NetworkMode"), "published": published, "exposed_only": unpublished,
             "networks": networks, "links": links, "neighbours": neighbours,
             "env_internal": internal, "env_external": external}
        if live and st.get("Running") and st.get("Pid"):
            ls = live_sockets(st["Pid"])
            conns = {}
            for rip, rport in ls["established"]:
                kind, other, _net = classify(rip, n, idents, ip_owner, gateways)
                conns.setdefault((rip, rport, kind, other), 0)
                conns[(rip, rport, kind, other)] += 1
            r["live"] = {"error": ls["error"], "listening": ls["listen"],
                         "connections": [{"remote": f"{k[0]}:{k[1]}", "kind": k[2], "container": k[3], "count": v}
                                         for k, v in sorted(conns.items())]}
        out.append(r)
    return out


def hostbind(ip, port):
    return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def render(report, nets, everyone):
    L = ["PUBLISHED PORTS (host side -> container)", ""]
    rows = [(hostbind(p["host_ip"], p["host_port"]), p["proto"], f"{r['name']}:{p['container_port']}", p["scope"])
            for r in report for p in r["published"]]
    if rows:
        head = ("HOST BIND", "PROTO", "CONTAINER:PORT", "REACHABLE FROM")
        w = [max(len(x[i]) for x in [head] + rows) for i in range(4)]
        fm = "  ".join("{:<%d}" % n for n in w)
        L.append(fm.format(*head))
        L += [fm.format(*x) for x in rows]
    else:
        L.append("(none)")
    L += ["", "NETWORKS", ""]
    for name, n in sorted(nets.items()):
        members = [f"{r['name']}={x['ip'] or '-'}" for r in everyone for x in r["networks"] if x["network"] == name]
        subnets = ", ".join(x.get("Subnet", "") for x in (n.get("IPAM") or {}).get("Config") or [] if x.get("Subnet"))
        flags = " internal-only" if n.get("Internal") else ""
        L.append(f"  {name}  [{n.get('Driver')}{flags}]  {subnets or '-'}")
        L.append(f"      members: {', '.join(members) if members else '(no containers)'}")
    for r in report:
        L += ["", "=" * 78, f"{r['name']}  ({r['state']}, image {r['image']})", "=" * 78]
        if r["network_mode"] in ("host", "none") or (r["network_mode"] or "").startswith("container:"):
            L.append(f"  network mode: {r['network_mode']}")
        L.append("  Published ports:")
        L += [f"    {hostbind(p['host_ip'], p['host_port'])} -> {p['container_port']}/{p['proto']}   [{p['scope']}]"
              for p in r["published"]] or ["    (none)"]
        if r["exposed_only"]:
            L.append("  Exposed, not published: " + ", ".join(r["exposed_only"]))
        L.append("  Networks:")
        for x in r["networks"] or []:
            sub = f"{x['ip']}/{x['prefix']}" if x["ip"] else "(no ip: not running)"
            L.append(f"    {x['network']}  [{x['driver']}{', internal-only' if x['internal'] else ''}]  {sub}"
                     f"  gw {x['gateway'] or '-'}  host iface {x['bridge_iface'] or '-'}")
            if x["aliases"]:
                L.append(f"        aliases: {', '.join(x['aliases'])}")
        if not r["networks"]:
            L.append("    (none)")
        L.append("  Internal links:")
        got = False
        for l in r["links"]:
            L.append(f"    --link {l}")
            got = True
        for e in r["env_internal"]:
            L.append(f"    env {e['var']} -> {e['container']}" + (f":{e['port']}" if e["port"] else "")
                     + f"  (container on network {e['network']})")
            got = True
        for net, nb in r["neighbours"].items():
            if nb:
                L.append(f"    same network '{net}': " + ", ".join(f"{x['name']} ({x['ip'] or '-'})" for x in nb))
                got = True
        if not got:
            L.append("    (none)")
        L.append("  Outside references (from environment variable names / hosts):")
        for e in r["env_external"]:
            where = {"external": "EXTERNAL", "lan": "private/LAN address", "local": "local",
                     "docker-host": "docker host"}.get(e["kind"], e["kind"])
            L.append(f"    env {e['var']} -> {e['host']}" + (f":{e['port']}" if e["port"] else "") + f"   [{where}]")
        if not r["env_external"]:
            L.append("    (none)")
        if "live" in r:
            lv = r["live"]
            L.append("  Live sockets right now:")
            if lv["error"]:
                L.append(f"    {lv['error']}")
            else:
                L.append("    listening on: " + (", ".join(str(p) for p in lv["listening"]) or "(nothing)"))
                for cn in lv["connections"]:
                    who = {"internal": f"container {cn['container']}", "docker-host": "docker host",
                           "lan": "private/LAN", "local": "local", "external": "EXTERNAL", "self": "itself"}[cn["kind"]]
                    L.append(f"    -> {cn['remote']}  x{cn['count']}  [{who}]")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Ports and links of Docker containers")
    ap.add_argument("names", nargs="*")
    ap.add_argument("--context")
    ap.add_argument("--live", action="store_true", help="read live sockets from /proc (root, on the docker host)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not shutil.which("docker"):
        sys.exit("ERROR: docker CLI not found")
    docker = make_docker(a.context)
    ids = docker("ps", "-aq").split()
    containers = inspect(docker, "container", ids)
    nets = {n["Name"]: n for n in inspect(docker, "network", docker("network", "ls", "-q").split())}
    if a.names:
        for n in a.names:
            if n not in {c["Name"].lstrip("/") for c in containers}:
                print(f"warning: no container named '{n}'", file=sys.stderr)
    everyone = analyse(containers, nets, a.live)
    report = [r for r in everyone if not a.names or r["name"] in a.names]
    if not report:
        print("No containers found.")
        return
    print(json.dumps(report, indent=2) if a.json else render(report, nets, everyone))


if __name__ == "__main__":
    main()
