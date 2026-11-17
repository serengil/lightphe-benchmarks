"""
Orchestrates the LightPHE cross-language benchmark.

Every (run, language) pair is executed in its own process so that
  * a key generation that never finishes can be killed (hard timeout),
  * peak resident memory of that pair can be read from the OS (wait4 rusage),
  * one language never shares a heap / JIT state with another case.

Languages are interleaved per run (py, go, java, ts, then the next run), so slow drift
of the machine (thermal state, background load) spreads over all languages evenly.

Usage:
    python run.py                         # everything, resumes where it stopped
    python run.py --langs go,java --only Paillier
    python run.py --redo                  # ignore previous results
"""
import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# folder holding the LightPHE, LightECC, lightphe-go, lightphe4j and lightphe-ts checkouts;
# defaults to a "github" folder next to this benchmark repository
GITHUB = os.environ.get("LIGHTPHE_GITHUB", os.path.join(os.path.dirname(HERE), "github"))
# interpreter with LightPHE installed; defaults to the one running this script
PYTHON = os.environ.get("LIGHTPHE_PYTHON", sys.executable)


def find_java_home():
    """JDK 17+ for lightphe4j: LIGHTPHE_JAVA_HOME, then lightphe4j's own finder, then JAVA_HOME"""
    if os.environ.get("LIGHTPHE_JAVA_HOME"):
        return os.environ["LIGHTPHE_JAVA_HOME"]
    finder = os.path.join(GITHUB, "lightphe4j", "scripts", "java-home.sh")
    if os.path.exists(finder):
        try:
            home = subprocess.check_output([finder], text=True, stderr=subprocess.DEVNULL).strip()
            if home and os.path.exists(os.path.join(home, "bin", "java")):
                return home
        except (subprocess.CalledProcessError, OSError):
            pass
    home = os.environ.get("JAVA_HOME")
    return home if home and os.path.exists(os.path.join(home, "bin", "java")) else None


JAVA_HOME = find_java_home()
JAVA = os.path.join(JAVA_HOME, "bin", "java") if JAVA_HOME else "java"
PREFIX = "@@BENCH "

LANGS = ["python", "go", "java", "ts"]

TIMING = {
    "max_tries": 10000,  # library default, bounds randomized key searches
    "kg_reps": 10,       # at most 10 key generations ...
    "kg_budget": 60,     # ... or until 60 s are spent (at least one)
    "warm_s": 1.0,       # warm up each op for 1 s ...
    "warm_max": 1000,    # ... or 1000 calls, whichever comes first (at least one)
    "op_reps": 100,      # at most 100 timed calls ...
    "op_budget": 10,     # ... or stop after 10 s once op_min samples exist
    "op_min": 3,
}


def command(lang):
    if lang == "python":
        return [PYTHON, "-I", os.path.join(HERE, "drivers/python/bench.py")]
    if lang == "go":
        return [os.path.join(HERE, "drivers/go/bench")]
    if lang == "java":
        with open(os.path.join(HERE, "drivers/java/classpath.txt"), encoding="utf-8") as f:
            cp = f.read().strip()
        return [JAVA, "-cp",
                os.path.join(HERE, "drivers/java/build") + ":" + cp, "Bench"]
    if lang == "ts":
        return ["node", os.path.join(HERE, "drivers/ts/bench.mjs")]
    raise ValueError(lang)


def run_one(lang, run, timeout, baseline=False):
    args = dict(TIMING)
    if baseline:
        args.update({"mode": "baseline"})
    else:
        args.update({
            "algorithm": run["algorithm"],
            "key_size": run["key_size"] if run["key_size"] is not None else "none",
            "form": run["form"] or "",
            "curve": run["curve"] or "",
            "m1": run["m1"], "m2": run["m2"], "k": run["k"],
            "ops": ",".join(run["ops"]),
            "sizes": "1" if lang == "python" else "0",
        })
    argv = command(lang) + [f"{k}={v}" for k, v in args.items()]

    started = time.time()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    events, stderr_tail = [], []

    def read_stdout():
        for line in proc.stdout:
            if line.startswith(PREFIX):
                events.append(json.loads(line[len(PREFIX):]))

    def read_stderr():
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())
            del stderr_tail[:-20]

    readers = [threading.Thread(target=read_stdout, daemon=True),
               threading.Thread(target=read_stderr, daemon=True)]
    for t in readers:
        t.start()

    timed_out = threading.Event()

    def kill():
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout, kill)
    timer.start()
    _, status, rusage = os.wait4(proc.pid, 0)
    timer.cancel()
    proc.returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
    for t in readers:
        t.join(timeout=5)

    # ru_maxrss is bytes on macOS, kilobytes on Linux
    maxrss = rusage.ru_maxrss if sys.platform == "darwin" else rusage.ru_maxrss * 1024
    done = any(e["ev"] == "done" for e in events)
    if timed_out.is_set():
        status_str = "timeout"
    elif not any(e["ev"] == "keygen" for e in events) and any(e["ev"] == "keygen_error" for e in events):
        status_str = "keygen_error"
    elif done:
        status_str = "ok"
    else:
        status_str = "crash"

    return {
        "lang": lang,
        "run": "__baseline__" if baseline else run["id"],
        "status": status_str,
        "wall_s": round(time.time() - started, 3),
        "timeout_s": timeout,
        "maxrss_bytes": maxrss,
        "user_cpu_s": rusage.ru_utime,
        "sys_cpu_s": rusage.ru_stime,
        "returncode": proc.returncode,
        "events": events,
        "stderr_tail": stderr_tail if status_str in ("crash", "keygen_error") else [],
        "timing": TIMING,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def load_cases():
    """config/cases.json, generated from gen_cases.py when missing"""
    path = os.path.join(HERE, "config", "cases.json")
    if not os.path.exists(path):
        sys.path.insert(0, HERE)
        import gen_cases  # pylint: disable=import-outside-toplevel
        gen_cases.main()
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def git_commit(repo):
    try:
        return subprocess.check_output(["git", "-C", repo, "log", "-1", "--format=%h %cI %s"],
                                       text=True).strip()
    except Exception:  # pylint: disable=broad-except
        return None


def environment():
    def out(cmd):
        try:
            return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
        except Exception:  # pylint: disable=broad-except
            return None

    return {
        "date": datetime.now(timezone.utc).isoformat(),
        "cpu": out(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "cores_logical": os.cpu_count(),
        "memory_bytes": int(out(["sysctl", "-n", "hw.memsize"]) or 0),
        "os": f"{platform.system()} {platform.release()} ({out(['sw_vers', '-productVersion'])})",
        "python": out([PYTHON, "--version"]),
        "go": out(["go", "version"]),
        "java": out([JAVA, "-version"]),
        "node": out(["node", "--version"]),
        "commits": {
            "LightPHE (python)": git_commit(f"{GITHUB}/LightPHE"),
            "LightECC (python dep)": git_commit(f"{GITHUB}/LightECC"),
            "lightphe-go": git_commit(f"{GITHUB}/lightphe-go"),
            "lightphe4j": git_commit(f"{GITHUB}/lightphe4j"),
            "lightphe-ts": git_commit(f"{GITHUB}/lightphe-ts"),
        },
        "timing": TIMING,
    }


def load_done(path):
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec["status"] != "crash":
                    done.add(rec["run"])
    return done


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--langs", default=",".join(LANGS))
    parser.add_argument("--only", default=None, help="substring filter on run id")
    parser.add_argument("--roles", default="tier,sweep,learning")
    parser.add_argument("--timeout", type=float, default=900, help="seconds per (run, language)")
    parser.add_argument("--redo", action="store_true")
    parser.add_argument("--quick", action="store_true", help="pilot: one sample per measurement")
    parser.add_argument("--skip-slow-from", default=None,
                        help="raw dir of a pilot: (run, language) pairs that timed out there are not "
                             "attempted again but recorded as excluded_slow")
    parser.add_argument("--out", default=os.path.join(HERE, "results/raw"))
    opts = parser.parse_args()

    if opts.quick:
        TIMING.update({"kg_reps": 1, "kg_budget": 0, "warm_s": 0, "warm_max": 1,
                       "op_reps": 1, "op_budget": 0, "op_min": 1})
    langs = opts.langs.split(",")
    roles = set(opts.roles.split(","))
    os.makedirs(opts.out, exist_ok=True)
    runs = load_cases()["runs"]
    runs = [r for r in runs if roles & set(r["roles"])]
    if opts.only:
        runs = [r for r in runs if opts.only in r["id"]]

    with open(os.path.join(opts.out, "environment.json"), "w", encoding="utf-8") as f:
        json.dump(environment(), f, indent=1)

    paths = {lang: os.path.join(opts.out, f"{lang}.jsonl") for lang in langs}
    if opts.redo:
        for p in paths.values():
            if os.path.exists(p):
                os.remove(p)
    done = {lang: load_done(paths[lang]) for lang in langs}

    def record(rec):
        with open(paths[rec["lang"]], "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        done[rec["lang"]].add(rec["run"])

    for lang in langs:
        if "__baseline__" not in done[lang]:
            rec = run_one(lang, None, 120, baseline=True)
            record(rec)
            print(f"[baseline] {lang}: {rec['maxrss_bytes'] / 2**20:.1f} MiB", flush=True)

    slow = {}
    if opts.skip_slow_from:
        for lang in langs:
            path = os.path.join(opts.skip_slow_from, f"{lang}.jsonl")
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    rec = json.loads(line)
                    if rec["status"] == "timeout":
                        stage = ("key generation" if not any(e["ev"] == "keygen" for e in rec["events"])
                                 else "operations")
                        slow[(lang, rec["run"])] = {"pilot_timeout_s": rec["timeout_s"], "stage": stage}

    total = len(runs) * len(langs)
    i = 0
    for run in runs:
        for lang in langs:
            i += 1
            if run["id"] in done[lang]:
                continue
            if (lang, run["id"]) in slow:
                info = slow[(lang, run["id"])]
                record({"lang": lang, "run": run["id"], "status": "excluded_slow", "wall_s": 0,
                        "timeout_s": info["pilot_timeout_s"], "maxrss_bytes": 0, "user_cpu_s": 0,
                        "sys_cpu_s": 0, "returncode": None, "events": [], "stderr_tail": [],
                        "excluded": f"pilot did not finish {info['stage']} within "
                                    f"{info['pilot_timeout_s']:.0f} s with a single sample",
                        "timing": TIMING, "finished_at": datetime.now(timezone.utc).isoformat()})
                print(f"[{i}/{total}] {run['id']:<45} {lang:<6} excluded_slow (pilot timeout)", flush=True)
                continue
            rec = run_one(lang, run, opts.timeout)
            record(rec)
            kg = [e["ns"] for e in rec["events"] if e["ev"] == "keygen"]
            fails = [e["op"] for e in rec["events"] if e["ev"] == "check" and not e["ok"]]
            print(f"[{i}/{total}] {run['id']:<45} {lang:<6} {rec['status']:<12} "
                  f"wall={rec['wall_s']:>8.1f}s kg={len(kg)} "
                  f"rss={rec['maxrss_bytes'] / 2**20:.0f}MiB"
                  + (f" CHECK-FAIL={fails}" if fails else ""), flush=True)


if __name__ == "__main__":
    main()
