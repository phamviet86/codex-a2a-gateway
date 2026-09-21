# Pinned Hermes source fixtures

These unmodified files are from `plugins/platforms/a2a/` at Hermes commit
`490e6b5966de330285905f52ebaee23c842dc3ea`, copied from the deployed checkout and
verified byte-for-byte against its Git objects. They are offline test fixtures,
not modules installed by the gateway wheel. Original and patched SHA-256 values
are recorded in `../hermes-a2a-compat.json`. Upstream licensing is in `LICENSE`.

Do not run these fixtures as an alternate Hermes installation. Tests apply the
published patch to a temporary Git checkout and exercise the resulting source.
