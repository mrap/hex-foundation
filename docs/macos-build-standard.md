# macOS build and signing standard

Native installation uses one machine policy and one shared signing transaction.
Compiling a binary is not installation. Cargo test executables and temporary
build outputs do not need individual certificate setup.

## Machine policy

The canonical public policy is:

```text
~/Library/Application Support/Hex/build-signing/policy.json
```

It selects an existing Apple Developer ID Application identity by certificate
fingerprint and Team ID. It may name its keychain. It contains no private key,
password or token. Do not copy it into each repository or change global Cargo
or linker settings to add signing.

The user authorizes Apple's signing tool to use the key once. Signing still
requires an available keychain. Service start and installed-app verification
use public certificate expectations and do not require the signing key.

## Supported entrypoints

| Entrypoint | Managed macOS behavior |
|---|---|
| Foundation `install.sh`, Hex source build | Publish through the common app transaction. |
| Foundation `install.sh`, BOI pinned source build | Verify the selected source and publish through the same transaction. |
| `hex upgrade` | Verify before a shortcut or rebuild, preserve personal features, then use the common transaction. |
| Hex service start, restart and recovery | Verify the installed app and helpers before changing a service. |
| BOI daemon start and restart | Verify the installed app and helpers before changing a service. |
| Managed prebuilt download | Reject until downloaded bytes have verifiable source provenance. |
| Code-intel binaries | Preserve existing files. Their separate app and service integration is not yet implemented. |

Hex and BOI retain distinct bundle identifiers. Their command-line paths point
into the installed bundles, not into Cargo output folders. Each installation
records the actual app hashes and source revision. It also records the exact
helper bytes used to verify future service starts.

All managed install callers use `system/scripts/macos-app-install.py`. That
transaction uses its fixed `macos-signing.py` sibling. A missing helper, invalid
policy or failed verification is an error, not permission to copy an unsigned
binary. An incomplete transaction blocks installation and service changes until
its ownership and recovery state are resolved.

Genuinely unconfigured legacy installs keep their existing behavior. Once a
valid policy or signed installation exists, removing configuration does not
authorize an unsigned replacement. Linux and App Store products retain their
own build requirements.

## Adding a product

Add a reviewed fixed product mapping and a stable, distinct bundle identifier.
Use the existing policy, signer and transaction. Wire every supported install
entrypoint to it. A background service must use the verified owner and hold the
product lock until its service change completes. Do not add a separate signing
script or select a signer through the environment.

Verify the actual caller paths, not only the helper in isolation. Include failed
builds, missing policies and interrupted transactions. Then qualify real signed
artifacts and the loaded service on macOS. For privacy retention, test a second
different build through the same installed owner.

Signing tests do not prove notarization, privacy grants or running-process
identity. Certificate renewal also needs a reviewed migration of the public
policy and installed expectations. Do not relax verification to make an old
certificate pass a new policy.

See [candidate signing](macos-signing.md),
[installation and recovery](macos-app-install.md), and
[shell install callers](macos-install-callers.md) for component contracts.
