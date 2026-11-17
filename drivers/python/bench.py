"""
LightPHE Python benchmark driver.

Usage: python bench.py key=value ...  (see run.py for the argument list)
Emits one JSON event per line prefixed with "@@BENCH " on stdout.
"""
import json
import sys
import time
import platform

PREFIX = "@@BENCH "


def emit(**event):
    sys.stdout.write(PREFIX + json.dumps(event) + "\n")
    sys.stdout.flush()


def parse_args(argv):
    args = dict(a.split("=", 1) for a in argv)
    for key in ["max_tries", "m1", "m2", "k", "kg_reps", "warm_max", "op_reps", "op_min"]:
        if key in args:
            args[key] = int(args[key])
    for key in ["kg_budget", "warm_s", "op_budget"]:
        if key in args:
            args[key] = float(args[key])
    args["key_size"] = int(args["key_size"]) if args.get("key_size") not in (None, "", "none") else None
    args["form"] = args.get("form") or None
    args["curve"] = args.get("curve") or None
    args["ops"] = [o for o in args.get("ops", "").split(",") if o]
    return args


OP_METHODS = {
    "add": lambda cs, c1, c2, k: cs.add(c1, c2),
    "multiply": lambda cs, c1, c2, k: cs.multiply(c1, c2),
    "multiply_by_constant": lambda cs, c1, c2, k: cs.multiply_by_constant(c1, k),
    "xor": lambda cs, c1, c2, k: cs.xor(c1, c2),
    "and": lambda cs, c1, c2, k: cs.homomorphic_and(c1, c2),
    "reencrypt": lambda cs, c1, c2, k: cs.reencrypt(c1),
}


def expected_values(op, m1, m2, k):
    return {
        "add": [m1 + m2],
        "multiply": [m1 * m2],
        # multiplicative schemes raise the plaintext to the constant instead
        "multiply_by_constant": [m1 * k, m1**k],
        "xor": [m1 ^ m2],
        "and": [m1 & m2],
        "reencrypt": [m1],
    }[op]


def measure(fn, args):
    """warm up, then time individual calls until rep count or time budget is reached"""
    start = time.perf_counter()
    warm = 0
    while True:
        fn()
        warm += 1
        if time.perf_counter() - start >= args["warm_s"] or warm >= args["warm_max"]:
            break
    samples = []
    start = time.perf_counter()
    while len(samples) < args["op_reps"]:
        t0 = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - t0)
        if len(samples) >= args["op_min"] and time.perf_counter() - start >= args["op_budget"]:
            break
    return warm, samples


# ---------------------------------------------------------------- sizes


def int_bytes(v):
    return max(1, (abs(v).bit_length() + 7) // 8)


def raw_size(item):
    """sum of minimal big-endian byte lengths of every integer in a structure"""
    if isinstance(item, bool) or item is None or isinstance(item, str):
        return 0
    if isinstance(item, int):
        return int_bytes(item)
    if isinstance(item, (list, tuple)):
        return sum(raw_size(i) for i in item)
    if isinstance(item, dict):
        return sum(raw_size(v) for v in item.values())
    if hasattr(item, "x") and hasattr(item, "y"):
        return raw_size(item.x) + raw_size(item.y)
    if hasattr(item, "__dict__"):
        return raw_size(vars(item))
    raise TypeError(f"cannot size {type(item)}")


def json_size(item):
    from lightphe.commons import serialization

    return len(json.dumps(serialization.encode(item), separators=(",", ":")))


def sizes(cs, c1):
    keys = cs.keys
    public = {k: v for k, v in keys.items() if k != "private_key"}
    return {
        "public_key_raw": raw_size(public),
        "private_key_raw": raw_size(keys.get("private_key")),
        "ciphertext_raw": raw_size(c1),
        "public_key_json": json_size(public),
        "keys_json": json_size(keys),
        "ciphertext_json": json_size(c1),
    }


# ---------------------------------------------------------------- main


def main():
    args = parse_args(sys.argv[1:])
    import lightphe
    from lightphe import LightPHE

    emit(ev="start", lang="python", runtime=platform.python_version(),
         library=getattr(lightphe, "__version__", "unknown"))
    if args.get("mode") == "baseline":
        emit(ev="done")
        return

    def build():
        return LightPHE(algorithm_name=args["algorithm"], key_size=args["key_size"],
                        form=args["form"], curve=args["curve"], max_tries=args["max_tries"])

    phe = None
    start = time.perf_counter()
    for _ in range(args["kg_reps"]):
        t0 = time.perf_counter_ns()
        try:
            phe = build()
        except Exception as err:  # pylint: disable=broad-except
            # randomized searches can exhaust max_tries: record it, keep sampling
            emit(ev="keygen_error", msg=str(err)[:300], ns=time.perf_counter_ns() - t0)
            if time.perf_counter() - start >= args["kg_budget"]:
                break
            continue
        emit(ev="keygen", ns=time.perf_counter_ns() - t0)
        if time.perf_counter() - start >= args["kg_budget"]:
            break
    if phe is None:
        emit(ev="done")
        return

    cs = phe.cs
    m1, m2, k = args["m1"], args["m2"], args["k"]
    pm = getattr(cs, "plaintext_modulo", None)

    cm = getattr(cs, "ciphertext_modulo", None)
    emit(ev="params", pm_bits=pm.bit_length() if isinstance(pm, int) else None,
         cm_bits=cm.bit_length() if isinstance(cm, int) else None)

    c1 = cs.encrypt(m1)
    c2 = cs.encrypt(m2)

    warm, samples = measure(lambda: cs.encrypt(m1), args)
    emit(ev="op", op="encrypt", warmup=warm, ns=samples)
    warm, samples = measure(lambda: cs.decrypt(c1), args)
    emit(ev="op", op="decrypt", warmup=warm, ns=samples)
    emit(ev="check", op="decrypt", ok=check(cs.decrypt(c1), [m1], pm))

    for op in args["ops"]:
        fn = OP_METHODS[op]
        try:
            result = fn(cs, c1, c2, k)
        except Exception as err:  # pylint: disable=broad-except
            emit(ev="op_unsupported", op=op, msg=str(err)[:300])
            continue
        try:
            got = cs.decrypt(result)
            emit(ev="check", op=op, ok=check(got, expected_values(op, m1, m2, k), pm), got=str(got))
        except Exception as err:  # pylint: disable=broad-except
            emit(ev="check", op=op, ok=False, got="error: " + str(err)[:200])
        warm, samples = measure(lambda: fn(cs, c1, c2, k), args)
        emit(ev="op", op=op, warmup=warm, ns=samples)

    if args.get("sizes") == "1":
        emit(ev="sizes", **sizes(cs, c1))
    emit(ev="done")


def check(got, expected, pm):
    if got in expected:
        return True
    if pm:
        return any(got % pm == e % pm for e in expected)
    return False


if __name__ == "__main__":
    main()
