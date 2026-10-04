# ramsched

A compiler launcher that schedules parallel builds by memory, not just by job count.

`-j` is a CPU limit. A build where most files need 300 MB but a few need 4 GB either runs
slowly at a low `-j` or runs out of memory at a high one. `ramsched` lets you keep
`-j` at the core count: each compile reserves memory before it starts, and is paused rather
than allowed to push the machine into swap or the OOM killer.

Two limits, usable separately or together (the stricter wins):

- **Headroom** (default): keep this much memory available on the machine, whoever uses the
  rest. Compiles back off when other programs grow.
- **Budget**: compiles together reserve at most this much.

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

Sizes are in GB.

| Variable | Default | |
|---|---|---|
| `RAMSCHED` | | `off` runs the compiler directly |
| `RAMSCHED_HEADROOM` | 10% of RAM (or cgroup limit) | memory kept available; `off` for budget only |
| `RAMSCHED_BUDGET` | none | total compiles may reserve |
| `RAMSCHED_PAUSE_BELOW` | headroom / 2 | available memory below which compiles pause |
| `RAMSCHED_KILL_BELOW` | headroom / 4 | available memory below which compiles are killed and requeued; `off` never kills |
| `RAMSCHED_DEFAULT` | `2` for C++, `0.5` otherwise | reservation for a command with no history |
| `RAMSCHED_HISTORY` | `~/.cache/ramsched/history.json` | per-file peak history |
| `RAMSCHED_DIR` | `$XDG_RUNTIME_DIR/ramsched`, else `/tmp/ramsched-<uid>` | ledger of running compiles, shared by all of the user's builds; must be private to the user |

## How it works

1. **Estimate.** The reservation is the file's highest recent peak plus 15%. A file with
   no history takes the median of its directory, else the default.
2. **Admit.** A compile starts when its reservation fits: within the budget, and leaving
   the headroom available after every running compile's unused reservation is counted.
   A compile that has waited over 30 s gets its share held back from newer ones, so large
   files are not starved by a stream of small ones.
3. **Watch.** The compile's memory (macOS `phys_footprint`, Linux RSS, summed over its
   process tree) is polled every 100 ms. At the reservation it grows if the limits allow,
   or is paused with `SIGSTOP` until they do.
4. **Defend the headroom.** Below the pause level every compile but the oldest pauses;
   below the kill level the newest is killed and requeued, one every 2 s, which is the
   only thing that frees memory. The oldest compile is never paused or killed for
   pressure, so a build always progresses.
5. **Break deadlocks.** If every compile holding memory is paused, the one that has run
   the shortest is killed and requeued with a larger reservation.
6. **Learn.** The peak is recorded under the object path relative to the build directory,
   so every checkout of a project shares one history.

Launchers coordinate through a lock file; a dead launcher's compile is killed by the next
one to take the lock. The ledger directory must be owned by the user with mode 0700, and
a process is only signalled if its start time matches the one recorded, so neither another
user nor a reused pid can direct a kill. Any internal error runs the compiler directly, so a fault costs
parallelism, never the build.

## Available memory

- **Linux**: `MemAvailable`, or the enclosing cgroup's limit minus its usage when lower.
  It counts free memory and cache the kernel can drop.
- **macOS**: the system's own available percentage (`kern.memorystatus_level`, what
  `memory_pressure` reports as free), plus the kernel's pressure level: *warn* pauses and
  *critical* kills. macOS keeps the percentage up by compressing other programs' memory,
  so it trails real allocations by seconds and understates them; the pressure level is
  what catches the rest. Headroom on macOS is therefore softer than on Linux: a build can
  push other programs into compression before ramsched reacts.

## Limits

- A waiting compile still holds one of the build tool's job slots. Run `-j` somewhat
  above the core count so light files keep the CPUs busy around it.
- Memory is polled, so a compile can overshoot its reservation by one interval's growth.
  The 15% margin absorbs it.
- The first build has no history and reserves conservatively.
