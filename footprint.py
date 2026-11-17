"""
Static package / deployment footprint of each LightPHE port.
Writes results/footprint.json.

Measured per language:
  * library artifact size (what a user downloads for the library itself)
  * transitive runtime dependencies and their installed size
  * a deployable artifact: Go's statically linked binary of the benchmark driver
  * source lines of code (non-blank, non-comment-only), tests excluded
"""
import json
import os
import subprocess
import tempfile
import importlib.metadata as md

HERE = os.path.dirname(os.path.abspath(__file__))
# folder holding the LightPHE, LightECC, lightphe-go, lightphe4j and lightphe-ts checkouts;
# defaults to a "github" folder next to this benchmark repository
GITHUB = os.environ.get("LIGHTPHE_GITHUB", os.path.join(os.path.dirname(HERE), "github"))


def dir_size(path, skip=("__pycache__", "tests", "test")):
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f.endswith(".pyc"):
                continue
            total += os.path.getsize(os.path.join(root, f))
    return total


def loc(path, exts, comment_prefixes, skip_dirs=("tests", "test", "__pycache__", "node_modules"),
        skip_suffixes=("_test.go",)):
    total, files = 0, 0
    for root, dirs, fs in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in fs:
            if not f.endswith(exts) or f.endswith(skip_suffixes):
                continue
            files += 1
            in_block = False
            with open(os.path.join(root, f), encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    s = line.strip()
                    if not s:
                        continue
                    if exts == (".py",):
                        if s.startswith('"""') or s.startswith("'''"):
                            if not (len(s) > 3 and s.endswith(s[:3])):
                                in_block = not in_block
                            continue
                        if in_block:
                            continue
                    else:
                        if in_block:
                            if "*/" in s:
                                in_block = False
                            continue
                        if s.startswith("/*"):
                            in_block = "*/" not in s
                            continue
                    if s.startswith(comment_prefixes):
                        continue
                    total += 1
    return {"files": files, "loc": total}


def python_dist_size(name):
    """installed size of a distribution from its RECORD file"""
    dist = md.distribution(name)
    size = 0
    for f in dist.files or []:
        p = f.locate()
        if os.path.isfile(p) and not str(p).endswith(".pyc"):
            size += os.path.getsize(p)
    return size


def python():
    # runtime closure: lightphe -> sympy, tqdm, lightecc ; sympy -> mpmath
    deps = {}
    for name in ["sympy", "mpmath", "tqdm", "lightecc"]:
        try:
            deps[name] = {"version": md.version(name), "installed_bytes": python_dist_size(name)}
        except md.PackageNotFoundError:
            # local editable checkout
            path = f"{GITHUB}/LightECC/lightecc"
            deps[name] = {"version": "local", "installed_bytes": dir_size(path)}
    return {
        "library_bytes": dir_size(f"{GITHUB}/LightPHE/lightphe"),
        "runtime_dependencies": deps,
        "runtime_dependency_count": len(deps),
        "runtime_dependency_bytes": sum(d["installed_bytes"] for d in deps.values()),
        "source": loc(f"{GITHUB}/LightPHE/lightphe", (".py",), ("#",)),
        # Go/Java/TS ports bundle their elliptic curve code, Python gets it from LightECC
        "source_lightecc": loc(f"{GITHUB}/LightECC/lightecc", (".py",), ("#",)),
        "note": "library_bytes = source tree without __pycache__; lightecc is a sibling library by the same author",
    }


def go():
    d = os.path.join(HERE, "drivers/go")
    with tempfile.TemporaryDirectory() as tmp:
        stripped = os.path.join(tmp, "bench")
        subprocess.check_call(["go", "build", "-ldflags=-s -w", "-o", stripped, "."], cwd=d)
        stripped_size = os.path.getsize(stripped)
        hello = os.path.join(tmp, "hello")
        os.makedirs(hello + "_src")
        with open(os.path.join(hello + "_src", "main.go"), "w", encoding="utf-8") as f:
            f.write('package main\nimport "fmt"\nfunc main(){fmt.Println("hi")}\n')
        with open(os.path.join(hello + "_src", "go.mod"), "w", encoding="utf-8") as f:
            f.write("module hello\ngo 1.17\n")
        subprocess.check_call(["go", "build", "-ldflags=-s -w", "-o", hello, "."], cwd=hello + "_src")
        hello_size = os.path.getsize(hello)
    return {
        "library_bytes": dir_size(f"{GITHUB}/lightphe-go/lightphe", skip=()) - sum(
            os.path.getsize(os.path.join(r, f))
            for r, _, fs in os.walk(f"{GITHUB}/lightphe-go/lightphe") for f in fs if f.endswith("_test.go")),
        "runtime_dependency_count": 0,
        "runtime_dependency_bytes": 0,
        "static_binary_bytes": stripped_size,
        "hello_world_binary_bytes": hello_size,
        "binary_overhead_bytes": stripped_size - hello_size,
        "source": loc(f"{GITHUB}/lightphe-go/lightphe", (".go",), ("//",)),
        "note": "library_bytes = Go sources without tests; binary = stripped benchmark driver (all 26 schemes linked)",
    }


def java():
    jar = f"{GITHUB}/lightphe4j/target/lightphe4j-0.0.29.jar"
    with open(os.path.join(HERE, "drivers/java/classpath.txt"), encoding="utf-8") as f:
        cp = [p for p in f.read().strip().split(":") if p and p != jar]
    # slf4j-simple is only the log binding the benchmark driver uses (test scope in pom.xml)
    cp = [p for p in cp if "slf4j-simple" not in p]
    deps = {os.path.basename(p): os.path.getsize(p) for p in cp}
    return {
        "library_bytes": os.path.getsize(jar),
        "runtime_dependencies": deps,
        "runtime_dependency_count": len(deps),
        "runtime_dependency_bytes": sum(deps.values()),
        "source": loc(f"{GITHUB}/lightphe4j/src/main", (".java",), ("//", "*")),
        "note": "library_bytes = compiled jar; lombok is compile-time only (provided scope)",
    }


def ts():
    root = f"{GITHUB}/lightphe-ts"
    with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
        pkg = json.load(f)
    deps = pkg.get("dependencies") or {}
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.check_output(["npm", "pack", "--json", "--pack-destination", tmp], cwd=root,
                                      text=True, stderr=subprocess.DEVNULL)
        info = json.loads(out)[0]
    return {
        "library_bytes": dir_size(os.path.join(root, "dist", "esm"), skip=()),
        "npm_tarball_bytes": info["size"],
        "npm_unpacked_bytes": info["unpackedSize"],
        "runtime_dependency_count": len(deps),
        "runtime_dependency_bytes": 0,
        "source": loc(os.path.join(root, "src"), (".ts",), ("//", "*")),
        "note": "library_bytes = one module format (dist/esm); the npm package ships esm + cjs + types",
    }


def main():
    out = {"python": python(), "go": go(), "java": java(), "ts": ts()}
    path = os.path.join(HERE, "results", "footprint.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
