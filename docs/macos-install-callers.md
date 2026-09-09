# macOS install callers

On macOS, `install.sh` delegates signed BOI and Hex publication to the common
`system/scripts/macos-app-install.py` transaction. The shell installer does not
publish an app bundle, write app state, or decide whether an install is legacy
or signed.

The caller invokes these commands with `/usr/bin/python3 -I -B`:

```text
mode PRODUCT --root ROOT
preflight PRODUCT --root ROOT
verify-current PRODUCT --root ROOT
install PRODUCT --root ROOT --source PATH --version VERSION --source-revision REVISION --helper-source-revision FOUNDATION_REVISION
```

Configured and previously signed products stay on the common transaction path.
The caller verifies the current app before a same-version BOI fast path and
compares it with the pinned BOI tag revision. A managed source build keeps its
managed status through the build and fails if signed evidence disappears.
Signer or transaction failure does not fall back to a raw destination copy.
Managed Hex prebuilt downloads are unsupported until the release bytes have
verifiable source provenance. Legacy unconfigured macOS installs may still use
the raw prebuilt path. The product revision and Foundation helper revision are
recorded separately.

Non-macOS behavior stays unchanged. A truly unconfigured legacy macOS install
keeps its existing raw path until the common mode command reports a managed
state. Code-intel binaries remain outside this caller's product map.
