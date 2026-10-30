#!/usr/bin/env python3
"""
docker-health.py - health report for every container on a Docker host.

Per container it checks: run state, healthcheck status (and the last check's output), exit
code, OOM kills, restart count / crash loops, restart-policy mismatches, engine errors and,
optionally, a TCP connect test of every published port.
Read-only: only docker ps / inspect / info / logs calls.

  python3 docker-health.py                      # all containers
  python3 docker-health.py moodle mariadb       # only these
  python3 docker-health.py --probe --logs 5     # test published ports; last 5 log lines of problems
  python3 docker-health.py --json               # machine readable
  ssh user@server 'python3 - --probe' < docker-health.py

Verdicts: OK, WARN, FAIL, STOP (stopped cleanly, nothing wrong).
Exit code: 0 = no problems, 1 = warnings, 2 = failures (usable from cron / monitoring).
Options: --probe  --logs N  --json  --context NAME  [container names ...]
"""
import argparse, datetime, json, shutil, socket, subprocess, sys


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


def load_containers(docker, names):
    ids = docker("ps", "-aq").split()
    data = json.loads(docker("container", "inspect", *ids)) if ids else []
    if names:
        have = {c["Name"].lstrip("/") for c in data}
        for n in names:
            if n not in have:
                print(f"warning: no container named '{n}'", file=sys.stderr)
        data = [c for c in data if c["Name"].lstrip("/") in set(names)]
    return sorted(data, key=lambda c: c["Name"])


def parse_ts(s):
    if not s or s.startswith("0001-"):
        return None
    try:
        return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def dur(sec):
    if sec is None:
        return "-"
    sec = int(max(sec, 0))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def probe_ports(c):
    seen, out = set(), []
    for cport, binds in sorted((c["NetworkSettings"].get("Ports") or {}).items()):
        if not cport.endswith("/tcp"):
            continue
        for b in binds or []:
            ip = b.get("HostIp") or ""
            if ip in ("", "0.0.0.0", "::"):
                ip = "127.0.0.1"
            try:
                port = int(b.get("HostPort"))
            except (TypeError, ValueError):
                continue
            if (ip, port) in seen:
                continue
            seen.add((ip, port))
            try:
                with socket.create_connection((ip, port), timeout=2):
                    ok = True
            except OSError:
                ok = False
            out.append((f"{ip}:{port}", ok))
    return out


def check(c, now, probe):
    st, hc = c["State"], c["HostConfig"]
    name = c["Name"].lstrip("/")
    status = st.get("Status") or "unknown"
    hl = st.get("Health") or {}
    health = hl.get("Status") or "none"
    started = parse_ts(st.get("StartedAt"))
    live = status in ("running", "paused", "restarting")
    uptime = (now - started).total_seconds() if live and started else None
    restarts = c.get("RestartCount") or 0
    policy = (hc.get("RestartPolicy") or {}).get("Name") or "no"
    code = st.get("ExitCode") or 0
    level, notes = 0, []

    def bump(lv, msg):
        nonlocal level
        level = max(level, lv)
        notes.append(msg)

    if status == "running":
        if health == "unhealthy":
            last = (hl.get("Log") or [{}])[-1]
            out = " ".join((last.get("Output") or "").split())[:120]
            msg = f"unhealthy ({hl.get('FailingStreak', 0)} failed checks in a row)"
            bump(2, msg + (f": {out}" if out else ""))
        elif health == "starting":
            bump(1, "healthcheck has not passed yet")
        elif health == "none":
            notes.append("no healthcheck defined")
    elif status == "restarting":
        bump(2, f"restart loop (last exit code {code})")
    elif status == "paused":
        bump(1, "paused")
    elif status == "dead":
        bump(2, "dead - remove and recreate it")
    elif status == "created":
        if policy in ("always", "unless-stopped"):
            bump(1, f"created but never started (restart policy '{policy}')")
        else:
            notes.append("created but never started")
    elif status == "exited":
        if code in (0, 143):
            if policy in ("always", "unless-stopped"):
                bump(1, f"stopped, but restart policy is '{policy}'")
            else:
                notes.append(f"stopped cleanly (exit {code})")
        elif code == 137:
            bump(2, "exited with code 137 (killed: out of memory or SIGKILL)")
        else:
            bump(2, f"exited with code {code}")
    if st.get("OOMKilled"):
        bump(2, "last run was OOM-killed (memory limit too low?)")
    if st.get("Error"):
        bump(2, f"engine error: {st['Error']}")
    if restarts >= 3:
        if uptime is not None and uptime < 600:
            bump(2, f"crash looping: {restarts} restarts, up only {dur(uptime)}")
        else:
            bump(1, f"restarted {restarts} times since creation")
    probes = []
    if probe and status == "running":
        probes = probe_ports(c)
        for addr, ok in probes:
            if not ok:
                bump(1, f"published port {addr} is not accepting connections")
    if status in ("exited", "created") and level == 0:
        verdict = "STOP"
    else:
        verdict = ["OK", "WARN", "FAIL"][level]
    return {"name": name, "state": status, "health": health, "uptime": dur(uptime), "restarts": restarts,
            "verdict": verdict, "notes": notes, "ports": [{"addr": a, "open": o} for a, o in probes]}


def main():
    ap = argparse.ArgumentParser(description="Health report for Docker containers")
    ap.add_argument("names", nargs="*", help="only these containers")
    ap.add_argument("--context")
    ap.add_argument("--probe", action="store_true", help="TCP-connect each published port (run on the docker host)")
    ap.add_argument("--logs", type=int, default=0, help="show last N log lines of WARN/FAIL containers")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not shutil.which("docker"):
        sys.exit("ERROR: docker CLI not found")
    docker = make_docker(a.context)
    containers = load_containers(docker, a.names)
    if not containers:
        print("No containers found.")
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    results = [check(c, now, a.probe) for c in containers]

    if a.json:
        print(json.dumps(results, indent=2))
    else:
        info = (docker("info", "--format", "{{.Name}}|{{.ServerVersion}}", check=False) or "?|?").strip().split("|")
        print(f"Docker host: {info[0]}   engine {info[1] if len(info) > 1 else '?'}\n")
        head = ("NAME", "STATE", "HEALTH", "UPTIME", "RESTARTS", "VERDICT")
        rows = [(r["name"], r["state"], r["health"], r["uptime"], str(r["restarts"]), r["verdict"]) for r in results]
        w = [max(len(x[i]) for x in [head] + rows) for i in range(len(head))]
        line = "  ".join("{:<%d}" % n for n in w)
        print(line.format(*head))
        for row, r in zip(rows, results):
            print(line.format(*row))
            for note in r["notes"]:
                print(f"    - {note}")
            if a.logs and r["verdict"] in ("WARN", "FAIL"):
                lg = subprocess.run(docker.base + ["logs", "--tail", str(a.logs), r["name"]],
                                    capture_output=True, text=True)
                for ln in (lg.stdout + lg.stderr).splitlines()[-a.logs:]:
                    print(f"    | {ln}")
        count = {k: sum(1 for r in results if r["verdict"] == k) for k in ("OK", "WARN", "FAIL", "STOP")}
        print(f"\n{len(results)} containers: {count['OK']} ok, {count['WARN']} warn, "
              f"{count['FAIL']} fail, {count['STOP']} stopped")
    worst = max((2 if r["verdict"] == "FAIL" else 1 if r["verdict"] == "WARN" else 0) for r in results)
    sys.exit(worst)


if __name__ == "__main__":
    main()
