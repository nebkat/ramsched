# ramsched

A compiler launcher that schedules parallel builds by memory, not just by job count.

`-j` is a CPU limit. A build where most files need 300 MB but a few need 4 GB either runs
slowly at a low `-j` or runs out of memory at a high one. `ramsched` lets you keep
`-j` at the core count: each compile reserves memory from a machine-wide budget before it
starts, and is paused rather than allowed to push the machine into swap or the OOM killer.

Unix only (Linux, macOS). Python 3.11+, standard library only, one file.

## Use

Put `ramsched` in front of the compiler. Anything that runs one process per compile
works:

```sh
# ccache (only cache misses go through it)
export CCACHE_PREFIX=/path/to/ramsched

# CMake
cmake -DCMAKE_C_COMPILER_LAUNCHER=/path/to/ramsched \
      -DCMAKE_CXX_COMPILER_LAUNCHER=/path/to/ramsched ...

# Make, Meson, Autotools
CC="/path/to/ramsched gcc" CXX="/path/to/ramsched g++" ...

# Cargo
export RUSTC_WRAPPER=/path/to/ramsched
```

`ramsched --report` summarises the last builds: how many compiles waited, grew,
paused or were killed, and the heaviest files.

| Variable | Default | |
|---|---|---|
| `RAMSCHED_BUDGET` | RAM (or cgroup limit) minus reserve | GB available to compiles; `off` passes straight through |
| `RAMSCHED_RESERVE` | `4` | GB kept back when the budget is derived from RAM |
| `RAMSCHED_DEFAULT` | `2` for C++, `0.5` otherwise | GB reserved for a command with no history |
| `RAMSCHED_HISTORY` | `~/.cache/ramsched/history.json` | per-file peak history |
| `RAMSCHED_DIR` | `$XDG_RUNTIME_DIR/ramsched`, else `/tmp/ramsched-<uid>` | ledger of running compiles, shared by all of the user's builds; must be private to the user |

## How it works

1. **Estimate.** The reservation is the file's highest recent peak plus 15%. A file with
   no history takes the median of its directory, else the default.
2. **Admit.** A compile starts when its reservation fits the budget. A compile that has
   waited over 30 s gets its share held back from newer ones, so large files are not
   starved by a stream of small ones.
3. **Watch.** The compile's memory (macOS `phys_footprint`, Linux RSS, summed over its
   process tree) is polled every 100 ms. At the reservation it grows, if the budget
   allows, or is paused with `SIGSTOP` until another compile finishes.
4. **Break deadlocks.** If every compile holding memory is paused, the one that has run
   the shortest is killed and requeued with a larger reservation. This is the only case
   that loses work.
5. **Learn.** The peak is recorded under the object path relative to the build directory,
   so every checkout of a project shares one history.

Launchers coordinate through a lock file; a dead launcher's compile is killed by the next
one to take the lock. The ledger directory must be owned by the user with mode 0700, and
a process is only signalled if its start time matches the one recorded, so neither another
user nor a reused pid can direct a kill. Any internal error runs the compiler directly, so a fault costs
parallelism, never the build.

## Limits

- A waiting compile still holds one of the build tool's job slots. Run `-j` somewhat
  above the core count so light files keep the CPUs busy around it.
- Memory is polled, so a compile can overshoot its reservation by one interval's growth.
  The 15% margin absorbs it.
- The first build has no history and reserves conservatively.
