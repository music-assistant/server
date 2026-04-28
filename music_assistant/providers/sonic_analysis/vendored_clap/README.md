# Vendored Microsoft CLAP

This directory contains a vendored copy of the `msclap` package from
[microsoft/CLAP](https://github.com/microsoft/CLAP), MIT licensed.

## Provenance

- **Upstream repository**: https://github.com/microsoft/CLAP
- **Pinned commit**: `e8a6467b87cd85716e20c6a008126150d9740be0`
- **Vendored**: 2026-04-22

## Why vendored instead of `pip install msclap`

The `msclap` PyPI package pins `librosa<0.11.0` and `numpy<2.0.0` in its
dependency metadata. These pins conflict with Music Assistant's upstream
pins (`librosa==0.11.0`, `numpy==2.3.5`), making a standard install
impossible. Inspection of `clap_wrapper.py` shows it does not actually
use librosa at runtime (it uses torchaudio), so the declared pin is
effectively unused — but pip refuses to install regardless.

Vendoring the source bypasses the dep-resolver conflict entirely. The MIT
license explicitly permits this.

## Modifications from upstream

- `CLAPWrapper.py` renamed to `clap_wrapper.py` to match our snake_case
  convention.
- `__init__.py` adjusted to reference the new module filename.
- `clap_wrapper.py::read_audio()` — swapped `torchaudio.load(audio_path)`
  for `librosa.load()`. Reason: torchaudio 2.11 delegates file I/O to
  `torchcodec`, which requires bundled FFmpeg shared libraries not
  available in standard Windows Python installs. librosa's soundfile/
  audioread backends are already in MA's dep tree and work across all
  supported platforms. Search for `MA MOD:` to find the exact change.

To update to a newer upstream version, re-copy the `msclap/`
subdirectory from the pinned commit above and re-apply the three
modifications above.

## License

MIT-licensed by Microsoft. The full license text is consolidated into the
repository root `NOTICE` file alongside other vendored third-party code.
See "Modifications from upstream" above for the MA-side changes.
