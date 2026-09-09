# macOS app install transaction

`system/scripts/macos-app-install.py` owns filesystem publication for the Hex,
BOI, `scipd`, and `cq` app bundles. It does not sign, verify signatures, or
invoke services.
Callers inject the accepted signer boundary.

## Public API

The caller supplies a fixed product name, its product root, a build artifact,
and a signer adapter:

```text
install(product, root, source, signer, policy_path=central_policy_path())
detect_mode(product, root, policy_path, signer)
```

The signer adapter exposes `stage(source, product, policy, candidate, receipt)`
and `verify_installed(bundle, product, policy, expected_state)`. It owns all
code-signing and Apple metadata checks. The transaction component
compares the public result with state but does not reproduce cryptographic
checks.

For service consumers, use a read-only `service_owner` wrapper that holds the
product lock before calling `verify_installed`. The wrapper returns the fixed
bundle path, executable path, compatibility path, product ID, generation,
public signature expectations, current hashes, and helper provenance. A
caller that already holds the lock must pass the inherited lock context rather
than acquire a nested lock.

## Fixed locations

The machine policy is shared:

`~/Library/Application Support/Hex/build-signing/policy.json`

Product state is separate:

- BOI: `~/.boi/BOI.app.install-state.json`
- Hex: `$HEX_DIR/.hex/Hex.app.install-state.json`
- scipd: `~/.codeintel/SCIPD.app.install-state.json`
- cq: `~/.codeintel/CQ.app.install-state.json`

The code-intel products share the `~/.codeintel` root but use independent
compatibility paths and helper directories:

- scipd: `bin/scipd`, `libexec/scipd`, bundle ID `com.mrap.hex.scipd`
- cq: `bin/cq`, `libexec/cq`, bundle ID `com.mrap.hex.cq`

Per-product transaction names are fixed under the product root:

- Lock: `.<product>.app-install.lock`
- Journal: `.<product>.app-install.journal.json`
- Rollback: `.<product>.app-install-rollback-<transaction_id>/`

The BOI standalone verifier is installed at `~/.boi/libexec/macos-signing.py`
as exact accepted Foundation helper bytes. Its public SHA-256 is recorded in
state. It resolves the same central policy by default. The installer copies
the helper, never the policy.

## State and modes

State schema version `1` records product, mode, fixed bundle ID, canonical app
and executable paths, compatibility path, Team ID, certificate fingerprint,
designated requirements, Mach-O UUIDs, app and executable hashes, generation,
source revision (the caller's Git SHA), signer helper hash, a `helpers` map containing both
`macos-signing.py` and `macos-app-install.py` hashes and source
revisions (their paths are fixed under the product's `libexec` directory),
previous compatibility identity, and transaction ID. It contains no secrets.

The supported modes are `empty`, `legacy-raw`, `configured-legacy`,
`signed-current`, `signed-policy-missing`, and `ambiguous`. A known signed
state with a missing central policy blocks replacement, downgrade, and service
ownership checks. The component never replays recorded signature metadata as
current verification. An unconfigured legacy install
remains unchanged until explicit signed migration.

## Transaction rules

The component acquires the lock with a nonblocking `flock`. Busy is an
immediate error. It holds the lock across staging, app publication,
compatibility-path publication, state commit, and recovery. It opens directory
descriptors for both the app parent and the `bin` compatibility parent and
revalidates device and inode identity before each swap.

Candidates stay beside the final app. A missing app uses macOS
`renameatx_np(..., RENAME_EXCL)`. An existing app uses
`renameatx_np(..., RENAME_SWAP)` with the candidate in the same parent. The
old app moves to a unique rollback directory with no-clobber publication. The
component never deletes or renames over an unknown app.

The legacy raw CLI path is migrated by swapping a prepared symlink or launcher
with the old regular file. The old raw file moves to the transaction rollback
directory. The path is never unlinked first. The state record commits only
after app and CLI paths point to the same candidate generation.

The bounded journal records schema, product, transaction ID, fixed-root paths,
old app and CLI identities, candidate hash, phase, and rollback names. An open
journal blocks a new transaction. Recovery may finish or roll back only when
the current app and CLI identities still match the journal candidate. If an
actor replaced either path, recovery stops loudly and preserves that actor's
replacement.

## Verification limits

Tests inject a fake signer for orchestration and use actual macOS filesystem
syscalls for no-clobber publication, directory swap, and raw-to-symlink
migration. They must assert that no `codesign`, `security`, `launchctl`, or
`systemctl` command runs. A skipped non-macOS syscall test is not an
atomicity qualification.

## CLI boundary

Commands use `COMMAND PRODUCT --root ROOT`:

```text
python3 macos-app-install.py mode boi --root ROOT
python3 macos-app-install.py preflight boi --root ROOT --lock-fd FD
python3 macos-app-install.py verify-current boi --root ROOT --lock-fd FD
python3 macos-app-install.py service-owner boi --root ROOT --lock-fd FD
python3 macos-app-install.py install boi --root ROOT --source SOURCE --version VERSION --source-revision PRODUCT_GIT_SHA --helper-source-revision FOUNDATION_GIT_SHA
```

The signer is always the regular `macos-signing.py` sibling beside this
script. Callers cannot select an arbitrary signing helper.

`service-owner` returns `mode: signed-current` after it verifies the installed
bundle, state, helper hashes, and current paths. It fails when the central
policy is absent. Service-changing callers must hold the product lock.

Read-only service commands return JSON with `schema_version`, `product`,
`mode`, `executable_path`, `bundle_identifier`, and `generation`, plus public
hashes and helper provenance. `--lock-fd` is inherited from the caller's
already-held product lock. The helper never acquires a nested lock.


## Final publication checks

The CLI accepts only the central machine policy path. An explicit `--policy`
may name that same path, but cannot select a product-specific policy. Unit-level
API fixtures may supply a disposable policy path. Policy validation always
executes the exact bounded, retained source bytes of the regular signer sibling.
A missing policy reader is an error, not permission to use a weaker validator.

Each copied helper is bounded to one MiB and checked against its recorded hash
before publication. The candidate verifier must report exactly the requested
version before any public path changes. The state records that verified version.
Hex publishes both `bin/hex` and `bin/hex-agent` in the same guarded transaction.

The child adapter clears inherited environment variables except HOME, a fixed
system PATH and locale. It drains stdout and stderr concurrently through pipes,
with a separate 64 KiB limit for each stream and one total deadline. No output
spools to disk. Timeout, output failure and synchronous cancellation signal the
owned process group while its leader remains unreaped, drain the pipes and reap
within a bounded cleanup interval. Normal leader exit with a descendant holding
a pipe cannot return success. This is cooperative child cleanup, not containment
of hostile processes that escape the group or close inherited pipes.

Before the committed journal marker, the transaction syncs the app parent,
compatibility parent, helper parent and rollback directory. Journal removal also
syncs the app parent. Failures remain loud, retain the journal and use the existing
identity-checked rollback. An actor replacement stops rollback without deleting
that actor's data. A partial publication is reported as such; it is not silently
removed or described as a clean install.
