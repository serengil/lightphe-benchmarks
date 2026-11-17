"""
Generates config/cases.json: the unique benchmark runs and the security tier table
that maps each (algorithm, tier) cell onto one of those runs.

Tiers follow NIST SP 800-57 equivalences:
    T80  ~ 80-bit  : RSA/DL 1024, ECC 160
    T112 ~ 112-bit : RSA/DL 2048, ECC 224
    T128 ~ 128-bit : RSA/DL 3072, ECC 256
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

TIERS = ["T80", "T112", "T128"]
MODULUS_BITS = {"T80": 1024, "T112": 2048, "T128": 3072}

# Freeman-BGN works in a pairing group with embedding degree 2, so the
# discrete log in F_{p^2} must match the RSA-equivalent modulus: |p| = modulus / 2.
FBGN_FIELD_BITS = {"T80": 512, "T112": 1024, "T128": 1536}

EC_CURVES = {
    "weierstrass": {"T80": "secp160r1", "T112": "p224", "T128": "p256"},
    "koblitz": {"T80": "k163", "T112": "k233", "T128": "k283"},
    # no standard ~160-bit Edwards curve exists in LightECC
    "edwards": {"T80": None, "T112": "e222", "T128": "ed25519"},
}

# Lattice schemes: the parameter is a dimension / ring degree, not a modulus size.
# Security is estimated with the HomomorphicEncryption.org standard table
# (lambda ~ 3.4 * n / log2 q for small secrets), see README. Each tier uses the
# smallest power-of-two dimension whose estimate meets the tier.
LATTICE = {
    # name: (log2 q, {tier: n})
    "Regev": (50, {"T80": 2048, "T112": 2048, "T128": 2048}),
    "Lyubashevsky-Peikert-Regev": (50, {"T80": 2048, "T112": 2048, "T128": 2048}),
    "Gentry-Sahai-Waters": (50, {"T80": 2048, "T112": 2048, "T128": 2048}),
    "Brakerski-Vaikuntanathan": (100, {"T80": 4096, "T112": 4096, "T128": 4096}),
}
LATTICE_SWEEP = {
    "Regev": [1024, 2048, 4096],
    "Lyubashevsky-Peikert-Regev": [1024, 2048, 4096],
    "Gentry-Sahai-Waters": [1024, 2048, 4096],
    "Brakerski-Vaikuntanathan": [2048, 4096, 8192],
}
# ML-KEM-512 is NIST category 1 (>= AES-128), so it already satisfies every tier.
MLKEM_TIER = {"T80": 512, "T112": 512, "T128": 512}
MLKEM_SWEEP = [512, 768, 1024]

# Candidate operations per algorithm (what the Python reference implements).
# Drivers record an op as unsupported if the library rejects it at runtime.
OPS = {
    "RSA": ["multiply"],
    "ElGamal": ["multiply", "multiply_by_constant", "reencrypt"],
    "Exponential-ElGamal": ["add", "multiply_by_constant", "reencrypt"],
    "EllipticCurve-ElGamal": ["add", "multiply_by_constant", "reencrypt"],
    "Paillier": ["add", "multiply_by_constant", "reencrypt"],
    "Damgard-Jurik": ["add", "multiply_by_constant", "reencrypt"],
    "Okamoto-Uchiyama": ["add", "multiply_by_constant", "reencrypt"],
    "Benaloh": ["add", "multiply_by_constant", "reencrypt"],
    "Naccache-Stern": ["add", "multiply_by_constant", "reencrypt"],
    "Goldwasser-Micali": ["xor", "reencrypt"],
    "Sander-Young-Yung": ["and", "reencrypt"],
    "Boneh-Goh-Nissim": ["add", "multiply", "multiply_by_constant", "reencrypt"],
    "Joye-Libert": ["add", "multiply_by_constant", "reencrypt"],
    "Castagnos-Laguillaumie": ["add", "multiply_by_constant", "reencrypt"],
    "Ishai-Paskin": ["add", "multiply_by_constant", "reencrypt"],
    "Damgard-Geisler-Kroigaard": ["add", "multiply_by_constant", "reencrypt"],
    "Bresson-Catalano-Pointcheval": ["add", "multiply_by_constant", "reencrypt"],
    "Camenisch-Shoup": ["add", "multiply_by_constant", "reencrypt"],
    "EllipticCurve-Paillier": ["add", "multiply_by_constant", "reencrypt"],
    "Regev": ["add", "multiply_by_constant", "reencrypt"],
    "Brakerski-Vaikuntanathan": ["add", "multiply", "multiply_by_constant", "reencrypt"],
    "Freeman-Boneh-Goh-Nissim": ["add", "multiply", "multiply_by_constant", "reencrypt"],
    "Gentry-Halevi-Vaikuntanathan": ["add", "multiply", "multiply_by_constant", "reencrypt"],
    "Lyubashevsky-Peikert-Regev": ["add", "multiply_by_constant", "reencrypt"],
    "ML-KEM": ["xor", "reencrypt"],
    "Gentry-Sahai-Waters": ["and", "multiply", "xor", "reencrypt"],
}

# Excluded after the pilot (user decision 2026-11-14), reported with the reason.
EXCLUDED = {
    "Benaloh": "Key generation samples random primes until the block size r (~1000-2000) divides p-1, "
               "i.e. ~r prime pairs per key. The pilot took 200 s at 1024 bits in TypeScript and did not "
               "finish 2048 bits within the limit. Go builds p = 1 mod r directly, so key generation "
               "differs algorithmically between ports.",
    "Naccache-Stern": "Randomized key search over smooth moduli. In the pilot Java exhausted max_tries=10000 "
                      "at 1024 bits, while Python (26 s) and Go (11 s) succeeded.",
}

# Run at the 80-bit tier only (user decision 2026-11-14).
SMALL_KEYS_ONLY = {
    "Boneh-Goh-Nissim": "not run: Python key generation took 208 s at 1024 bits and grows roughly "
                        "cubically with the modulus (same algorithm in every port)",
}

MODULUS_ALGOS = [
    "RSA", "ElGamal", "Exponential-ElGamal", "Paillier", "Damgard-Jurik",
    "Okamoto-Uchiyama", "Goldwasser-Micali",
    "Sander-Young-Yung", "Boneh-Goh-Nissim", "Joye-Libert", "Castagnos-Laguillaumie",
    "Ishai-Paskin", "Damgard-Geisler-Kroigaard", "Bresson-Catalano-Pointcheval",
    "Camenisch-Shoup", "EllipticCurve-Paillier",
]

# bit-wise schemes encrypt a single bit, so they are measured per bit
BITWISE = {"Goldwasser-Micali", "Sander-Young-Yung", "Gentry-Sahai-Waters"}

CATEGORY = {
    **{a: "number-theoretic" for a in MODULUS_ALGOS},
    "EllipticCurve-ElGamal": "elliptic-curve",
    "Freeman-Boneh-Goh-Nissim": "pairing",
    "Regev": "lattice", "Brakerski-Vaikuntanathan": "lattice",
    "Lyubashevsky-Peikert-Regev": "lattice", "Gentry-Sahai-Waters": "lattice",
    "ML-KEM": "lattice", "Gentry-Halevi-Vaikuntanathan": "lattice",
}

LEARNING_ONLY = {"Gentry-Halevi-Vaikuntanathan"}


def run_id(alg, key_size, form=None, curve=None):
    rid = f"{alg}|{key_size}"
    if curve:
        rid += f"|{form}:{curve}"
    return rid


def main():
    runs = {}
    tiers = []  # rows: algorithm label, tier, run id (or None) and a note

    def add_run(alg, key_size, form=None, curve=None, role="tier"):
        rid = run_id(alg, key_size, form, curve)
        if rid not in runs:
            bit = alg in BITWISE
            runs[rid] = {
                "id": rid,
                "algorithm": alg,
                "key_size": key_size,
                "form": form,
                "curve": curve,
                "category": CATEGORY[alg],
                "learning_only": alg in LEARNING_ONLY,
                "m1": 1 if bit else 17,
                "m2": 1 if bit else 23,
                "k": 3,
                "ops": OPS[alg],
                "roles": [role],
            }
        elif role not in runs[rid]["roles"]:
            runs[rid]["roles"].append(role)
        return rid

    for alg in MODULUS_ALGOS:
        for t in TIERS:
            if alg in SMALL_KEYS_ONLY and t != "T80":
                tiers.append({"label": alg, "algorithm": alg, "tier": t, "run": None,
                              "note": SMALL_KEYS_ONLY[alg]})
                continue
            tiers.append({"label": alg, "algorithm": alg, "tier": t,
                          "run": add_run(alg, MODULUS_BITS[t]), "note": None})

    for form, curves in EC_CURVES.items():
        for t in TIERS:
            curve = curves[t]
            label = f"EllipticCurve-ElGamal ({form})"
            if curve is None:
                tiers.append({"label": label, "algorithm": "EllipticCurve-ElGamal", "tier": t,
                              "run": None, "note": "no standard Edwards curve at this level"})
                continue
            rid = add_run("EllipticCurve-ElGamal", None, form, curve)
            tiers.append({"label": label, "algorithm": "EllipticCurve-ElGamal", "tier": t,
                          "run": rid, "note": curve})

    for t in TIERS:
        tiers.append({"label": "Freeman-Boneh-Goh-Nissim", "algorithm": "Freeman-Boneh-Goh-Nissim",
                      "tier": t, "run": add_run("Freeman-Boneh-Goh-Nissim", FBGN_FIELD_BITS[t]),
                      "note": f"|p|={FBGN_FIELD_BITS[t]}"})

    for alg, (logq, dims) in LATTICE.items():
        for t in TIERS:
            n = dims[t]
            tiers.append({"label": alg, "algorithm": alg, "tier": t, "run": add_run(alg, n),
                          "note": f"n={n}, log q={logq}, est. {round(3.4 * n / logq)}-bit"})
        for n in LATTICE_SWEEP[alg]:
            add_run(alg, n, role="sweep")

    for t in TIERS:
        tiers.append({"label": "ML-KEM", "algorithm": "ML-KEM", "tier": t,
                      "run": add_run("ML-KEM", MLKEM_TIER[t]), "note": "ML-KEM-512 (NIST cat. 1)"})
    for p in MLKEM_SWEEP:
        add_run("ML-KEM", p, role="sweep")

    # learning-only scheme with insecure toy parameters, run once and reported apart
    add_run("Gentry-Halevi-Vaikuntanathan", 4, role="learning")
    for t in TIERS:
        tiers.append({"label": "Gentry-Halevi-Vaikuntanathan", "algorithm": "Gentry-Halevi-Vaikuntanathan",
                      "tier": t, "run": None, "note": "learning purposes only, no secure parameters"})

    # cheap runs first so that a long run still yields most of the table early
    order = sorted(runs.values(), key=lambda r: (r["key_size"] or 0) > 2048)
    out = {"tiers": tiers, "runs": order, "excluded": EXCLUDED, "small_keys_only": SMALL_KEYS_ONLY}
    os.makedirs(os.path.join(HERE, "config"), exist_ok=True)
    with open(os.path.join(HERE, "config", "cases.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"{len(order)} unique runs, {len(tiers)} tier cells")


if __name__ == "__main__":
    main()
