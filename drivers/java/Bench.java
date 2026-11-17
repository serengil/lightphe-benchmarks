import io.github.serengil.lightphe.LightPHE;
import io.github.serengil.lightphe.model.Homomorphic;
import io.github.serengil.lightphe.model.value.CipherValue;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;

/**
 * LightPHE Java benchmark driver. Usage: java Bench key=value ...
 * Emits one JSON event per line prefixed with "@@BENCH " on stdout.
 */
public class Bench {

    static Map<String, String> a = new HashMap<>();

    static int i(String k) { return Integer.parseInt(a.get(k)); }

    static double d(String k) { return Double.parseDouble(a.get(k)); }

    static String q(String s) {
        return "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ") + "\"";
    }

    static void emit(String body) {
        System.out.println("@@BENCH {" + body + "}");
        System.out.flush();
    }

    static String trim(String s) {
        if (s == null) return "null";
        return s.length() > 300 ? s.substring(0, 300) : s;
    }

    static double secondsSince(long t0) { return (System.nanoTime() - t0) / 1e9; }

    static Object[] measure(Supplier<?> fn) {
        long start = System.nanoTime();
        int warm = 0;
        while (true) {
            fn.get();
            warm++;
            if (secondsSince(start) >= d("warm_s") || warm >= i("warm_max")) break;
        }
        List<Long> samples = new ArrayList<>();
        start = System.nanoTime();
        while (samples.size() < i("op_reps")) {
            long t0 = System.nanoTime();
            fn.get();
            samples.add(System.nanoTime() - t0);
            if (samples.size() >= i("op_min") && secondsSince(start) >= d("op_budget")) break;
        }
        return new Object[] {warm, samples};
    }

    static void emitOp(String op, Object[] m) {
        emit("\"ev\":\"op\",\"op\":" + q(op) + ",\"warmup\":" + m[0] + ",\"ns\":" + m[1].toString().replace(" ", ""));
    }

    static boolean check(BigInteger got, List<BigInteger> expected, BigInteger pm) {
        for (BigInteger e : expected) {
            if (got.equals(e)) return true;
            if (pm != null && pm.signum() > 0 && got.mod(pm).equals(e.mod(pm))) return true;
        }
        return false;
    }

    public static void main(String[] argv) {
        for (String kv : argv) {
            int p = kv.indexOf('=');
            a.put(kv.substring(0, p), kv.substring(p + 1));
        }
        emit("\"ev\":\"start\",\"lang\":\"java\",\"runtime\":" + q(System.getProperty("java.version"))
                + ",\"library\":" + q(LightPHE.VERSION));
        if ("baseline".equals(a.get("mode"))) {
            emit("\"ev\":\"done\"");
            return;
        }

        String ks = a.get("key_size");
        Integer keySize = (ks == null || ks.isEmpty() || ks.equals("none")) ? null : Integer.valueOf(ks);
        String form = a.getOrDefault("form", "");
        String curve = a.getOrDefault("curve", "");

        LightPHE phe = null;
        long start = System.nanoTime();
        for (int r = 0; r < i("kg_reps"); r++) {
            long t0 = System.nanoTime();
            try {
                LightPHE built = LightPHE.builder()
                        .algorithmName(a.get("algorithm"))
                        .keySize(keySize)
                        .form(form.isEmpty() ? null : form)
                        .curve(curve.isEmpty() ? null : curve)
                        .maxTries(i("max_tries"))
                        .build();
                phe = built;
            } catch (RuntimeException err) {
                emit("\"ev\":\"keygen_error\",\"msg\":" + q(trim(String.valueOf(err.getMessage())))
                        + ",\"ns\":" + (System.nanoTime() - t0));
                if (secondsSince(start) >= d("kg_budget")) break;
                continue;
            }
            emit("\"ev\":\"keygen\",\"ns\":" + (System.nanoTime() - t0));
            if (secondsSince(start) >= d("kg_budget")) break;
        }
        if (phe == null) {
            emit("\"ev\":\"done\"");
            return;
        }

        Homomorphic cs = phe.cs();
        BigInteger m1 = BigInteger.valueOf(i("m1"));
        BigInteger m2 = BigInteger.valueOf(i("m2"));
        BigInteger k = BigInteger.valueOf(i("k"));
        BigInteger pm = cs.plaintextModulo();

        BigInteger cm = cs.ciphertextModulo();
        emit("\"ev\":\"params\",\"pm_bits\":" + (pm == null ? "null" : pm.bitLength())
                + ",\"cm_bits\":" + (cm == null ? "null" : cm.bitLength()));

        CipherValue c1 = cs.encrypt(m1);
        CipherValue c2 = cs.encrypt(m2);

        emitOp("encrypt", measure(() -> cs.encrypt(m1)));
        emitOp("decrypt", measure(() -> cs.decrypt(c1)));
        emit("\"ev\":\"check\",\"op\":\"decrypt\",\"ok\":" + check(cs.decrypt(c1), List.of(m1), pm));

        Map<String, Supplier<CipherValue>> ops = new HashMap<>();
        ops.put("add", () -> cs.add(c1, c2));
        ops.put("multiply", () -> cs.multiply(c1, c2));
        ops.put("multiply_by_constant", () -> cs.multiplyByConstant(c1, k));
        ops.put("xor", () -> cs.xor(c1, c2));
        ops.put("and", () -> cs.homomorphicAnd(c1, c2));
        ops.put("reencrypt", () -> cs.reencrypt(c1));

        Map<String, List<BigInteger>> expected = new HashMap<>();
        expected.put("add", List.of(m1.add(m2)));
        expected.put("multiply", List.of(m1.multiply(m2)));
        expected.put("multiply_by_constant", List.of(m1.multiply(k), m1.pow(k.intValue())));
        expected.put("xor", List.of(m1.xor(m2)));
        expected.put("and", List.of(m1.and(m2)));
        expected.put("reencrypt", List.of(m1));

        for (String op : a.getOrDefault("ops", "").split(",")) {
            if (op.isEmpty()) continue;
            Supplier<CipherValue> fn = ops.get(op);
            CipherValue res;
            try {
                res = fn.get();
            } catch (RuntimeException err) {
                emit("\"ev\":\"op_unsupported\",\"op\":" + q(op) + ",\"msg\":" + q(trim(String.valueOf(err.getMessage()))));
                continue;
            }
            try {
                BigInteger got = cs.decrypt(res);
                emit("\"ev\":\"check\",\"op\":" + q(op) + ",\"ok\":" + check(got, expected.get(op), pm)
                        + ",\"got\":" + q(got.toString()));
            } catch (RuntimeException err) {
                emit("\"ev\":\"check\",\"op\":" + q(op) + ",\"ok\":false,\"got\":" + q("error: " + trim(err.getMessage())));
            }
            emitOp(op, measure(fn));
        }
        emit("\"ev\":\"done\"");
    }
}
