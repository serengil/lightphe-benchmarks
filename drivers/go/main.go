// LightPHE Go benchmark driver. Usage: bench key=value ...
// Emits one JSON event per line prefixed with "@@BENCH " on stdout.
package main

import (
	"encoding/json"
	"fmt"
	"math/big"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/serengil/lightphe-go/lightphe"
	"github.com/serengil/lightphe-go/lightphe/phe"
)

func emit(ev map[string]interface{}) {
	b, _ := json.Marshal(ev)
	fmt.Println("@@BENCH " + string(b))
}

type args map[string]string

func (a args) int(k string) int {
	v, err := strconv.Atoi(a[k])
	if err != nil {
		panic(fmt.Sprintf("bad %s: %v", k, err))
	}
	return v
}

func (a args) float(k string) float64 {
	v, err := strconv.ParseFloat(a[k], 64)
	if err != nil {
		panic(fmt.Sprintf("bad %s: %v", k, err))
	}
	return v
}

func measure(fn func() error, a args) (int, []int64, error) {
	start := time.Now()
	warm := 0
	for {
		if err := fn(); err != nil {
			return 0, nil, err
		}
		warm++
		if time.Since(start).Seconds() >= a.float("warm_s") || warm >= a.int("warm_max") {
			break
		}
	}
	samples := []int64{}
	start = time.Now()
	for len(samples) < a.int("op_reps") {
		t0 := time.Now()
		if err := fn(); err != nil {
			return 0, nil, err
		}
		samples = append(samples, time.Since(t0).Nanoseconds())
		if len(samples) >= a.int("op_min") && time.Since(start).Seconds() >= a.float("op_budget") {
			break
		}
	}
	return warm, samples, nil
}

func check(got *big.Int, expected []*big.Int, pm *big.Int) bool {
	for _, e := range expected {
		if got.Cmp(e) == 0 {
			return true
		}
		if pm != nil && pm.Sign() > 0 {
			if new(big.Int).Mod(got, pm).Cmp(new(big.Int).Mod(e, pm)) == 0 {
				return true
			}
		}
	}
	return false
}

func main() {
	a := args{}
	for _, kv := range os.Args[1:] {
		parts := strings.SplitN(kv, "=", 2)
		a[parts[0]] = parts[1]
	}
	emit(map[string]interface{}{"ev": "start", "lang": "go", "runtime": runtime.Version(), "library": lightphe.Version})
	if a["mode"] == "baseline" {
		emit(map[string]interface{}{"ev": "done"})
		return
	}

	opts := []lightphe.Option{lightphe.WithMaxTries(a.int("max_tries"))}
	if ks := a["key_size"]; ks != "" && ks != "none" {
		opts = append(opts, lightphe.WithKeySize(a.int("key_size")))
	}
	if a["curve"] != "" {
		opts = append(opts, lightphe.WithCurve(a["form"], a["curve"]))
	}
	alg := phe.Algorithm(a["algorithm"])

	var sys *lightphe.Cryptosystem
	start := time.Now()
	for i := 0; i < a.int("kg_reps"); i++ {
		t0 := time.Now()
		c, err := lightphe.New(alg, opts...)
		if err != nil {
			msg := err.Error()
			if len(msg) > 300 {
				msg = msg[:300]
			}
			emit(map[string]interface{}{"ev": "keygen_error", "msg": msg, "ns": time.Since(t0).Nanoseconds()})
			if time.Since(start).Seconds() >= a.float("kg_budget") {
				break
			}
			continue
		}
		emit(map[string]interface{}{"ev": "keygen", "ns": time.Since(t0).Nanoseconds()})
		sys = c
		if time.Since(start).Seconds() >= a.float("kg_budget") {
			break
		}
	}
	if sys == nil {
		emit(map[string]interface{}{"ev": "done"})
		return
	}

	cs := sys.Scheme()
	m1 := big.NewInt(int64(a.int("m1")))
	m2 := big.NewInt(int64(a.int("m2")))
	k := big.NewInt(int64(a.int("k")))
	pm := cs.PlaintextModulo()

	bits := func(x *big.Int) interface{} {
		if x == nil {
			return nil
		}
		return x.BitLen()
	}
	emit(map[string]interface{}{"ev": "params", "pm_bits": bits(pm), "cm_bits": bits(cs.CiphertextModulo())})

	c1, err := cs.Encrypt(m1)
	if err != nil {
		panic(err)
	}
	c2, err := cs.Encrypt(m2)
	if err != nil {
		panic(err)
	}

	warm, s, err := measure(func() error { _, e := cs.Encrypt(m1); return e }, a)
	if err != nil {
		panic(err)
	}
	emit(map[string]interface{}{"ev": "op", "op": "encrypt", "warmup": warm, "ns": s})
	warm, s, err = measure(func() error { _, e := cs.Decrypt(c1); return e }, a)
	if err != nil {
		panic(err)
	}
	emit(map[string]interface{}{"ev": "op", "op": "decrypt", "warmup": warm, "ns": s})
	d, _ := cs.Decrypt(c1)
	emit(map[string]interface{}{"ev": "check", "op": "decrypt", "ok": check(d, []*big.Int{m1}, pm)})

	opFns := map[string]func() (phe.Value, error){
		"add":                  func() (phe.Value, error) { return cs.Add(c1, c2) },
		"multiply":             func() (phe.Value, error) { return cs.Multiply(c1, c2) },
		"multiply_by_constant": func() (phe.Value, error) { return cs.MultiplyByConstant(c1, k) },
		"xor":                  func() (phe.Value, error) { return cs.Xor(c1, c2) },
		"and":                  func() (phe.Value, error) { return cs.And(c1, c2) },
		"reencrypt":            func() (phe.Value, error) { return cs.Reencrypt(c1) },
	}
	mul := new(big.Int).Mul(m1, m2)
	expected := map[string][]*big.Int{
		"add":                  {new(big.Int).Add(m1, m2)},
		"multiply":             {mul},
		"multiply_by_constant": {new(big.Int).Mul(m1, k), new(big.Int).Exp(m1, k, nil)},
		"xor":                  {new(big.Int).Xor(m1, m2)},
		"and":                  {new(big.Int).And(m1, m2)},
		"reencrypt":            {m1},
	}

	for _, op := range strings.Split(a["ops"], ",") {
		if op == "" {
			continue
		}
		fn := opFns[op]
		res, err := fn()
		if err != nil {
			emit(map[string]interface{}{"ev": "op_unsupported", "op": op, "msg": err.Error()})
			continue
		}
		got, err := cs.Decrypt(res)
		if err != nil {
			emit(map[string]interface{}{"ev": "check", "op": op, "ok": false, "got": "error: " + err.Error()})
		} else {
			emit(map[string]interface{}{"ev": "check", "op": op, "ok": check(got, expected[op], pm), "got": got.String()})
		}
		warm, s, err := measure(func() error { _, e := fn(); return e }, a)
		if err != nil {
			panic(err)
		}
		emit(map[string]interface{}{"ev": "op", "op": op, "warmup": warm, "ns": s})
	}
	emit(map[string]interface{}{"ev": "done"})
}
