#!/usr/bin/env bash
# Builds the Go, Java and TypeScript benchmark drivers against local checkouts of the ports.
#   LIGHTPHE_GITHUB      folder with lightphe-go, lightphe4j, lightphe-ts (default: ../github)
#   LIGHTPHE_JAVA_HOME   JDK 17+ (default: lightphe4j/scripts/java-home.sh, then JAVA_HOME)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GITHUB="${LIGHTPHE_GITHUB:-$(cd "$HERE/.." && pwd)/github}"
echo "using ports in $GITHUB"

# --- Go: native binary (go.mod points at ../../../github/lightphe-go; override if elsewhere)
cd "$HERE/drivers/go"
if [ "$(cd "$GITHUB/lightphe-go" && pwd)" != "$(cd ../../.. && pwd)/github/lightphe-go" ]; then
  TMP="$(mktemp -d)"
  cp go.mod "$TMP/go.mod"; [ -f go.sum ] && cp go.sum "$TMP/go.sum"
  go mod edit -modfile="$TMP/go.mod" -replace "github.com/serengil/lightphe-go=$GITHUB/lightphe-go"
  go build -modfile="$TMP/go.mod" -o bench .
else
  go build -o bench .
fi
echo "go driver built ($(go version))"

# --- Java: lightphe4j jar, runtime classpath, compiled driver
JAVA_HOME="${LIGHTPHE_JAVA_HOME:-$("$GITHUB/lightphe4j/scripts/java-home.sh" 2>/dev/null || echo "${JAVA_HOME:-}")}"
export JAVA_HOME
(cd "$GITHUB/lightphe4j" && ./mvnw -q package -DskipTests)
JAR="$(ls "$GITHUB"/lightphe4j/target/lightphe4j-*.jar | grep -v -e sources -e javadoc | head -1)"
DEPS="$(mktemp)"
(cd "$GITHUB/lightphe4j" && ./mvnw -q dependency:build-classpath -Dmdep.outputFile="$DEPS" >/dev/null)
# runtime needs only slf4j-api plus a log binding; the rest of the listed classpath is test scope
echo "$JAR:$(tr ':' '\n' < "$DEPS" | grep slf4j | paste -sd: -)" > "$HERE/drivers/java/classpath.txt"
rm -f "$DEPS"
mkdir -p "$HERE/drivers/java/build"
"$JAVA_HOME/bin/javac" -cp "$(cat "$HERE/drivers/java/classpath.txt")" -d "$HERE/drivers/java/build" "$HERE/drivers/java/Bench.java"
echo "java driver built ($("$JAVA_HOME/bin/java" -version 2>&1 | head -1))"

# --- TypeScript: compiled dist of lightphe-ts (the driver itself is plain JS)
(cd "$GITHUB/lightphe-ts" && { [ -d node_modules ] || npm ci; } && npm run -s build >/dev/null)
echo "ts library built (node $(node --version))"
