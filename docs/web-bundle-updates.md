# Web manager bundle updates

The web manager is installed as immutable bundles under
`/opt/vhackintosh/web/bundles/<bundle-id>`. Each bundle contains the exact
`core`, API, agent, frontend build, and hash-locked Python dependencies used by
the services. `web/current` is the only activation pointer. The ISO packager
builds and validates the complete candidate in a temporary directory, copies it
beside the installed versions, and replaces that pointer with one atomic rename.

`BUNDLE.json` records the bundle format, REST contract version, local RPC
protocol version, supported SQLite jobs-schema interval, and exact Reims release
tag. `MANIFEST.sha256` covers every payload file including `BUNDLE.json`; the
bundle ID is derived from the payload and Reims release. Validation rejects
missing/extra files, symlinks, altered hashes, unsupported API/RPC contracts,
an out-of-range database schema, or a different Reims runtime release.

For an appliance update, `SystemUpdater.promote_web_bundle(candidate)` reads the
current jobs schema and installed Reims release, then holds the shared VM
maintenance lock through validation, promotion, and service restart. If either
service does not come back active, the updater atomically restores the previous
pointer and restarts the previous API/agent pair. Old immutable bundles remain
available for recovery; user databases and VM disks are never copied or rolled
back as part of a code update.

The updater accepts a prepared candidate directory. Transporting and
authenticating that candidate from a hosted release is a separate delivery
step; until that release pipeline supplies the bundle, the existing source
updater cannot manufacture a web bundle on the appliance because the live OS
does not carry the npm/uv build toolchain.
