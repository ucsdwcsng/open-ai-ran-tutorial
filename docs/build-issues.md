# Clean-clone build issues

Findings from bringing the `open-ai-ran-tutorial` branch up from scratch on a second
server (Aug 2026). Everything here was found the same way: **a fresh
clone plus the README, with no files inherited from an existing working tree.**

That method matters, because every issue below shares one shape:

> A build product is `.gitignore`d, the thing that would **regenerate** it is missing
> from the repo, and the original author's tree keeps working because their copy
> survives untracked. The repo looks healthy. A fresh clone cannot build.

None of these reproduce for someone who has built the tree before. All of them are
fatal for a new machine, a CI runner, or a new lab member.

| # | Issue | Repo | Severity | Status |
|---|---|---|---|---|
| 1 | `.gitignore` excludes Concurrency Kit's build templates | tutorial | blocker | fix included |
| 2 | Same defect in the OCUDU RAN | ocudu-jbpf | blocker | patch included |
| 3 | `bufsize` codelet has no Makefile | tutorial | blocker | fix included |
| 4 | README §5d codelet build list is incomplete | tutorial | blocker | fix included |
| 5 | Dashboard xApp dies silently on a missing module | tutorial | high | proposed |
| 6 | `slice_mgmt` needs a newer jbpf SDK — its xApp cannot load | tutorial | high | reported |
| 7 | `open5gs-dbctl add` is not idempotent | docs | low | doc note |

---

## 1. `.gitignore` excludes Concurrency Kit's build templates

**File:** `jrtc-apps/containers/Docker/srsRAN_Project/external/jbpf/.gitignore` line 4

```gitignore
*.o
*~
*.pyc
build          <-- matches 3p/ck/build/, which is SOURCE
```

In `.gitignore`, an unanchored pattern matches **at every level**, not just the repo
root. The only directory named `build` anywhere in that tree is:

```
external/jbpf/3p/ck/build/
```

which holds Concurrency Kit's 15 `configure` input templates — `ck.build.in`, the
per-architecture profiles (`ck.build.x86_64`, `ck.build.aarch64`, …), `ck.pc.in`,
`ck.spec.in`, `regressions.build.in`. These are **inputs**, not outputs.

### Why it is hard to diagnose

The failure is silent for a long time and then surfaces somewhere unrelated. With the
templates missing, ck's `configure` still exits **0**, having emitted only:

```
sed: can't read build/ck.build.in: No such file or directory
```

and written a **zero-byte** `build/ck.build`. `ALL_LIBS` is therefore empty, so `make`
reports `Nothing to be done for 'all'` — also success. The build proceeds for several
minutes and finally dies in the install step of a dependency:

```
cp: cannot stat 'src/libck.so': No such file or directory
make: *** [Makefile:51: install-so] Error 1
```

Nothing in that message points at a `.gitignore`.

### Fix

Anchor the rule to the directory the `.gitignore` sits in:

```diff
-build
+/build
```

This preserves the original intent (ignore jbpf's own top-level CMake output) and is
provably sufficient, because **ck already ships correct rules of its own** —
`3p/ck/.gitignore` lines 2–6:

```gitignore
build/ck.build
build/ck.pc
build/ck.spec
build/Makefile
build/regressions.build
```

ck deliberately tracks the 15 templates and ignores the 5 generated files. Once the
over-broad parent rule is anchored, ck's rules do exactly the right thing and no new
ignore entries are needed.

The 15 templates are added to the index in the same commit.

### Verifying

```bash
git ls-files jrtc-apps/containers/Docker/srsRAN_Project/external/jbpf/3p/ck/build | wc -l   # 15
git check-ignore -v .../3p/ck/build/ck.build                                                # ck's own rule
```

---

## 2. The same defect in `ocudu-jbpf`

**Repo:** `ucsdwcsng/open-ai-ran-ocudu-jbpf` — **file:** `external/jbpf/.gitignore` line 4

Byte-identical defect, and the OCUDU tree is worse off: its `3p/ck/build/` is **absent
entirely**, so `./build.sh` fails at the same `install-so` step. Both trees vendor
ck 0.7.1, so the same 15 templates apply.

**This blocks `ocudu-jbpf` on its own**, independent of this repo — anyone cloning it
to build the gNB hits it.

Fix is identical, and must be submitted to that repo. See
`tools/patches/ocudu-jbpf-gitignore.patch`.

---

## 3. `bufsize` codelet has no Makefile

> **Superseded.** The `bufsize` codeletsets were retired in favour of the runtime
> `bufctl` control hook, so this directory no longer exists. Kept as a record of the
> defect class: a codeletset yaml referencing objects the Makefile cannot produce.

**Directory:** `jrtc-apps/codelets/bufsize/`

Ships four files — `rlc_fixed.cpp`, `bufsize16.yaml`, `bufsize64.yaml`,
`bufsize256.yaml` — and no `Makefile`, so the README's build line fails:

```
make: *** No targets specified and no makefile found.  Stop.
```

It was never committed (no delete in history, not ignored), and the sibling `codel/`
added in the *same* commit does have one.

### Why the standard Makefile would not have been enough

The three codeletset yamls each reference a differently-sized object:

```yaml
codelet_path: ${JBPF_CODELETS}/bufsize/rlc_fixed16.o    # and 64, 256
```

`Makefile.defs` derives objects by wildcard — `CPP_OBJECTS = $(CPP_SOURCES:.cpp=.o)` —
so `rlc_fixed.cpp` can only ever produce `rlc_fixed.o`. Three objects from one source
is not expressible in the shared machinery, and no `make.sh` flag changes that. The
source expects per-variant defines:

```c
#ifndef FIXED_LIMIT
#define FIXED_LIMIT (65536)
#endif
```

`.gitignore` lists `*.o`, so the author's locally-built `rlc_fixed{16,64,256}.o` were
invisible to git and their tree kept working.

### Fix

`jrtc-apps/codelets/bufsize/Makefile` with explicit per-variant rules (included).
Sizes follow the source's own default — 64 → 65536 — so 16 → 16384 and 256 → 262144.
All three compile and pass the verifier at 36 instructions.

---

## 4. README §5d codelet build list is incomplete

**File:** `README.md` line 153

```bash
for d in rlc mac pdcp ngap rrc ue_contexts perf edgeric bufsize l4span; do ./make.sh -d "$d"; done
```

Missing six directories that loaded deployments require:

| Missing | Required by |
|---|---|
| `fapi_dl_conf`, `fapi_ul_conf`, `fapi_ul_crc`, `fapi_rach` | `jrtc_apps/dashboard/deployment_ocudu.yaml` (module imports) |
| `xran_packets` | same |
| `upt` | `jrtc_apps/upt/deployment_fixed.yaml`, the Grafana user-plane dashboard |

`deployment_ocudu.yaml` deliberately drops the FAPI/xran **codeletsets** (OCUDU's FAPI
structs differ from the SDK's) but still **imports their Python modules** — that is
intentional per its own header comment, so the modules must exist even though no
codelet attaches. The result is 6 module paths with 0 matching codeletsets, and if
those dirs were never built, the import fails.

### Fix

Extend the list to all directories the deployments reference (included).

### It regressed

The list went stale again when new codeletsets were added: `bufctl`, `codel` and
`slice_mgmt` all have a codeletset yaml but were absent from the loop. `bufctl` is the
one that bites -- it is the RLC buffer-control codelet the demo in README §12 drives, and
because `*.o` is gitignored a fresh clone has no `rlc_ctrl.o`, so `bufctl_cli.py` fails
its load with nothing obviously wrong in the clone.

The durable rule is **every directory with a codeletset yaml gets built** -- with one
documented exception. `slice_mgmt` has a codeletset yaml but `slice_mgmt.cpp` does not
compile against this SDK image:

```
slice_mgmt.cpp:119:75: error: use of undeclared identifier 'JBPF_MAX_SLICES'
... 20 errors generated.
make: *** [../Makefile.common:15: slice_mgmt.o] Error 1
```

So `slice_mgmt.o` has never existed and `slice_mgmt.yaml` cannot load. It stays out of
the build list until the source is fixed; putting it in makes the documented §5d step
fail for everyone. `tools/check-clean-clone.sh` check [5] only compares the list against
dirs whose Python modules an xApp imports, so it does not cover a codeletset whose
objects are needed but whose modules are not imported.

---

## 5. Dashboard xApp dies silently on a missing module

Consequence of #4, but worth fixing separately because of how it *presents*.

With `fapi_*` unbuilt, the app terminates during load:

```
Error: Failed to import module: fapi_gnb_dl_config_stats
ModuleNotFoundError: No module named 'fapi_gnb_dl_config_stats'
Python app terminated: /apps/dashboard/dashboard.py
```

But `jrtc-ctl load` still reports success:

```
level=info msg="loaded app" id=5
```

and the jbpf channels are created normally. From the operator's side the load looks
clean and the only symptom is **no telemetry at all** — with the real error buried in
the jrtc container log among thousands of unrelated lines.

**Proposed fix (not applied — needs a maintainer decision):** since the modules are
imported for streams that may legitimately carry no data, a missing one should
degrade rather than kill the app — log a warning, skip that stream, continue. Failing
that, `jrtc-ctl load` should report a non-zero status when the app terminates during
startup, so "loaded app" cannot be printed for an app that is already dead.

---

## 6. `slice_mgmt` does not compile against the pinned SDK

```
slice_mgmt.cpp:215:58: error: use of undeclared identifier 'JBPF_MAX_SLICES'
20 errors generated.
```

`JBPF_MAX_SLICES` is not defined in the `jbpf_protobuf` submodule pinned at
`bd009ef`. Either the submodule needs bumping to the SDK version this codelet was
written against, or the codelet needs to carry its own definition.

Not fixed here — the correct resolution depends on which SDK version is intended.

**This is worse than it first appears.** `slice_mgmt` is not merely an unused codelet:
`jrtc_apps/slice_mgmt/deployment.yaml` — the xApp added alongside it — imports
`${JBPF_CODELETS}/slice_mgmt/slice_mgmt.py` and loads its codeletset. Since the
codelet cannot compile, that module is never generated, so **the shipped `slice_mgmt`
xApp cannot load at all**, failing the same silent way as §5.

It is also absent from the README build list, so nothing tells you to try building it.
`tools/check-clean-clone.sh` catches this combination (check 5) — it was in fact the
check, not manual inspection, that surfaced it.

---

## 7. `open5gs-dbctl add` is not idempotent

README §3 says to add IMSIs …001–004. Re-running it on a core that already has some
subscribers **creates duplicates** rather than being a no-op:

```
2 999700000000001
2 999700000000002
1 999700000000003
1 999700000000004
```

Same K/OPC, so authentication still succeeds and the fault is easy to miss. Worth a
one-line note in the README, or a `showall | grep -c` guard before adding.

---

## What would have caught all of this

`tools/check-clean-clone.sh` — a static check that needs no build and no cluster. It
verifies that:

1. no `.gitignore` shadows a vendored `build/` directory containing `*.in` templates;
2. every codelet directory with a codeletset yaml has a Makefile;
3. every `.o` a codeletset references can actually be produced from a source file or
   an explicit Makefile rule;
4. every Python module an xApp deployment imports comes from a directory the README
   build list actually builds.

Items 1, 2, 3 and 4 correspond to issues 1/2, 3, 3 and 4/5 respectively. See
`RELEASE-NOTES.md` for where it fits in the release flow.
