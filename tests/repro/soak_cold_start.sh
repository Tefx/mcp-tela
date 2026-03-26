#!/bin/bash
# Soak test: repeated cold-start verification for persistent flakiness
# This script runs runtime liveness tests multiple times with cleanup between runs

set -eo pipefail

NUM_RUNS="${NUM_RUNS:-5}"
WORKTREE_ROOT="$(pwd)"
LOCKFILE="$HOME/.tela/gateway.lock"
PIDS_FILE="/tmp/tela_pids_$$.txt"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "========================================"
echo "Cold-Start Soak Verification"
echo "========================================"
echo "Runs: $NUM_RUNS"
echo "Lockfile: $LOCKFILE"
echo "Worktree: $WORKTREE_ROOT"
echo ""

# Cleanup function - kills any remaining tela processes
cleanup() {
    echo "Cleaning up stale processes..."
    
    # Clean lockfile if it references a dead process
    if [ -f "$LOCKFILE" ]; then
        pid=$(python3 -c "import json; print(json.load(open('$LOCKFILE')).get('pid', ''))" 2>/dev/null || true)
        if [ -n "$pid" ] && [ "$pid" -gt 0 ]; then
            if ! kill -0 "$pid" 2>/dev/null; then
                echo "  Removing stale lockfile (pid $pid is dead)"
                rm -f "$LOCKFILE"
            fi
        fi
    fi
    
    # Additional cleanup: find and kill any stale tela processes
    if [ -f "$PIDS_FILE" ]; then
        while read -r pid; do
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                echo "  Killing stale process $pid"
                kill -9 "$pid" 2>/dev/null || true
            fi
        done < "$PIDS_FILE"
        rm -f "$PIDS_FILE"
    fi
}

# Run cleanup before starting
cleanup

# Tracking results
PASSED=0
FAILED=0
RESULTS=()

run_test_iteration() {
    local run_number=$1
    local test_file="$WORKTREE_ROOT/tests/repro/test_connect_runtime_liveness.py"
    
    echo ""
    echo "========================================"
    echo "Run $run_number of $NUM_RUNS"
    echo "========================================"
    
    # Pre-cleanup
    rm -f "$LOCKFILE"
    rm -f "$PIDS_FILE"
    
    # Run tests with verbose output
    # Record start time
    start_time=$(date +%s.%N)
    
    if uv run python -m pytest "$test_file" -v --tb=short 2>&1 | tee "/tmp/soak_run_${run_number}.log"; then
        end_time=$(date +%s.%N)
        elapsed=$(echo "$end_time - $start_time" | bc)
        
        echo ""
        echo -e "${GREEN}RUN $run_number: PASSED${NC} (elapsed: ${elapsed}s)"
        RESULTS+=("run $run_number: PASSED (${elapsed}s)")
        PASSED=$((PASSED + 1))
    else
        end_time=$(date +%s.%N)
        elapsed=$(echo "$end_time - $start_time" | bc)
        
        echo ""
        echo -e "${RED}RUN $run_number: FAILED${NC} (elapsed: ${elapsed}s)"
        RESULTS+=("run $run_number: FAILED (${elapsed}s)")
        FAILED=$((FAILED + 1))
    fi
    
    # Post-run cleanup - ensure no orphaned processes
    # Check for tela processes and kill them
    pgrep -f "tela serve" >> "$PIDS_FILE" 2>/dev/null || true
    pgrep -f "tela connect" >> "$PIDS_FILE" 2>/dev/null || true
    
    while read -r pid; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "  Killing process $pid from run $run_number"
            kill -9 "$pid" 2>/dev/null || true
        fi
    done < "$PIDS_FILE"
    
    # Small delay between runs to ensure cleanup
    sleep 1
}

# Run soak iterations
for i in $(seq 1 "$NUM_RUNS"); do
    run_test_iteration "$i"
done

# Final cleanup
cleanup

# Summary
echo ""
echo "========================================"
echo "SOAK SUMMARY"
echo "========================================"
echo "Per-run results:"
for result in "${RESULTS[@]}"; do
    echo "  $result"
done
echo ""
echo "Total: $PASSED passed, $FAILED failed out of $NUM_RUNS runs"
echo ""

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}Residual flake verdict: NONE_OBSERVED${NC}"
    echo "All $NUM_RUNS cold-start iterations passed"
    exit 0
else
    echo -e "${RED}Residual flake verdict: REPRODUCED${NC}"
    echo "Flakiness observed in $FAILED out of $NUM_RUNS runs"
    exit 1
fi