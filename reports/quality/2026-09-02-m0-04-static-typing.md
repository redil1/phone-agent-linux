# M0-04 — Static typing boundary ratchet

Date: 2026-09-02 UTC

## Decision

PhoneAgent uses Pyright for boundary-first static typing. The development dependency is
`pyright[nodejs]>=1.1.411,<2`; the `nodejs` extra supplies a reproducible Node runtime and avoids
depending on a host Node installation or downloading one during each check.

The initial strict ratchet covers:

- `ai_bridge/control_plane.py`
- `ai_bridge/runtime_config.py`
- `qualification/device_qualification.py`
- `scripts/generate_baseline_corpus.py`

These files define external control contracts, authoritative runtime configuration, device
qualification evidence, and the protected corpus generator. New platform boundaries must join the
strict include list as they are introduced. Existing legacy modules remain measurable debt rather
than being silenced or allowed to block unrelated cleanup.

## Checker evaluation

Both candidates were evaluated in the production Python 3.11 image against the same four files.

| Candidate | Strict result before fixes | Cold/runtime behavior | Decision |
| --- | ---: | --- | --- |
| mypy 2.3.1 | 0 diagnostics in 12.25 s | Self-contained, but the strict preset allowed the relevant `Any` values to mask unsafe generic containers | Rejected for this ratchet |
| Pyright 1.1.411 | 19 diagnostics in 6.40 s; 2.19 s with bundled Node | Identified Pydantic default-factory inference, untyped JSON mappings, recursive container normalization, and an untyped credential list | Selected |

The initial Python-only Pyright installation attempted to bootstrap Node and failed in the
production image because `libatomic.so.1` was absent. The selected `nodejs` extra eliminates that
environmental failure. This was verified without modifying the production image.

## Changes made

- Added a strict `[tool.pyright]` boundary include in `pyproject.toml`.
- Added the self-contained Pyright development dependency and regenerated `uv.lock`.
- Replaced partially inferred Pydantic list factories with explicitly typed factories.
- Narrowed decoded JSON objects before returning authoritative qualification data.
- Typed recursive state normalization and the required-credential accumulator.
- Preserved runtime behavior: the changes are type narrowing or equivalent empty-list factories.

## Verification

- Strict Pyright: `0 errors, 0 warnings, 0 informations`.
- Configured Ruff: `All checks passed!`.
- Dependency lock check: 243 packages resolved consistently under CPython 3.11.15.
- Focused boundary regressions: 40 passed.
- Full isolated non-device gate: 837 passed, 40 skipped, 4 device tests deselected, 3 known
  deprecation warnings, 56.84 seconds.

The full check ran in a disposable writable clone, so source-adjacent tests could exercise file
permissions without mutating the working project or failing because of a read-only bind mount.

## Legacy inventory and ratchet policy

A strict, non-gating inventory of maintained Python roots analyzed 122 files and found 2,448 legacy
diagnostics. The largest categories were unknown member types (826), unknown argument types (595),
unknown variable types (538), and argument type mismatches (157). The largest files were the product
research extractor (241), web server (201), legacy ChatGPT Realtime pipeline (149), and Cascade
production pipeline (117).

This inventory is not suppressed. It establishes the cleanup baseline. S2S files are deleted in
Milestone 1 rather than typed; other modules enter the strict include list when their owning
milestone changes their boundary. The four protected boundary files cannot regress because every
plain `pyright` invocation checks them in strict mode.

## Reproducibility hashes

| File | SHA-256 |
| --- | --- |
| `pyproject.toml` | `656b42f3ce6bd5771fa3aa979512a06d63df559a02b50beb97b0ef611148fea1` |
| `uv.lock` | `f6ed99016fdd7a4d6ff5f9424c1d0f38e6686172e1e7a41f07364a5ce4dc9f5e` |
| `ai_bridge/control_plane.py` | `84d3489b82893d241a3936c8cbbdc38673429da23385fa73733b759b5fbb5ed1` |
| `ai_bridge/runtime_config.py` | `a90ced888281c67e2d75afdf6063ffa1b0c889468be06180bda562e3158d21b3` |
| `qualification/device_qualification.py` | `e47bdd0bc4ecc6fa1145770a31a4f993213e08b5ab3964176f0f493ab249a2a0` |
| `scripts/generate_baseline_corpus.py` | `1d31cddacc1f606438b1170eba92047283d970589afa6c7acaceffa8e4611d30` |

No production service was restarted, no Android package was changed, and no live call was placed for
this milestone.
