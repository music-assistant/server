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

All MA-side changes are marked `# MA MOD:` in source — `grep -rn "MA MOD:"
music_assistant/providers/sonic_analysis/vendored_clap/` lists every
divergence point.

**Renames (cosmetic):**
- `CLAPWrapper.py` renamed to `clap_wrapper.py` to match our snake_case
  convention.
- `__init__.py` adjusted to reference the new module filename.

**Audio loading (cross-platform fix):**
- `clap_wrapper.py::read_audio()` — swapped `torchaudio.load(audio_path)`
  for `librosa.load()`. Reason: torchaudio 2.11 delegates file I/O to
  `torchcodec`, which requires bundled FFmpeg shared libraries not
  available in standard Windows Python installs. librosa's soundfile/
  audioread backends are already in MA's dep tree and work across all
  supported platforms.

**Skip-GPT2 download path:**
- `clap_wrapper.py::CLAPWrapper.__init__` — added `text_enabled: bool = True`
  kwarg. When False, skips `AutoModel.from_pretrained` + `AutoTokenizer.from_pretrained`
  calls (~500MB GPT2 weights saved on default installs).
- `models/clap.py::CLAP` and `TextEncoder` — added `skip_text_encoder` /
  `skip_text_model` flags to support the above. When set, the text branch
  is constructed without loading any HF text model.
- `clap_wrapper.py::preprocess_text` — raises `RuntimeError` early when
  text encoder is disabled, with a clear actionable message.

**Standalone checkpoint fetch (keeps the download out of provider setup):**
- `clap_wrapper.py::CLAPWrapper.download_weights` — new classmethod wrapping the
  `hf_hub_download` call. The provider fetches the checkpoint through it ahead of
  time, so building a `CLAPWrapper` afterwards is a local `torch.load` rather than
  an unbounded network transfer inside `handle_async_init`'s timeout.
- `clap_wrapper.py::CLAPWrapper.__init__` — its own `if not model_fp` fetch now
  calls `download_weights` instead of `hf_hub_download` directly, keeping the repo
  and filename in one place.
- `clap_wrapper.py::CLAPWrapper.cached_weights` — new classmethod wrapping
  `try_to_load_from_cache`. Resolves an already-downloaded checkpoint without any
  network call, so a provider load that follows a completed download does not
  re-run `hf_hub_download`'s entry-tag revalidation and works offline.

**Tensor-input audio path (avoids re-loading audio for live playback):**
- `clap_wrapper.py::preprocess_audio_from_tensor` and
  `get_audio_embeddings_from_tensor` — accept pre-loaded tensors so the
  sonic_analysis provider can run CLAP inference on PCM it already has
  in memory, without round-tripping through a temp file.

**Warnings filter scoped to model load:**
- `clap_wrapper.py` — removed `warnings.filterwarnings("ignore")` from module scope.
  Upstream placed this call at module level, meaning it would install a permanent
  process-wide suppress-all entry into `warnings.filters` the first time the module was
  imported, silencing warnings from all other MA providers for the rest of the process
  lifetime.  Replaced with a `with warnings.catch_warnings(): warnings.filterwarnings("ignore")`
  block inside `load_clap()`, which restores the original filter state when the method returns.

**transformers v5 compatibility:**
- `clap_wrapper.py::preprocess_text` — `tokenizer.encode_plus(text=ttext, ...)`
  → `tokenizer(ttext, ...)`. Reason: `encode_plus()` was *removed* (not just
  deprecated) in transformers 5.x; v5+ pin is required for CVE-2026-1839.
  Same kwargs/return type, same behavior.

**Captioning model + 2022 audio model dropped:**
- `model_name` dict entries for `"2022"` and `"clapcap"` removed. MA only
  uses the 2023 audio model.
- `models/mapper.py` deleted (~200 lines of caption-generation transformer
  code that was only reachable via `load_clapcap`).
- `clap_wrapper.py::load_clapcap`, `generate_caption`, `_generate_beam`
  methods deleted. The captioning code path was never exercised by MA;
  shipping it would have meant ~250 lines of dead code in the upstream PR.
- `configs/config_2022.yml` and `configs/config_clapcap.yml` deleted.
- `models/__init__.py` does not import `mapper` (no change needed).

To re-add captioning support: re-vendor `models/mapper.py` from upstream's
pinned commit, restore the deleted methods in `clap_wrapper.py`, and
re-add the dict entries in `model_name`.

To update to a newer upstream version: re-copy the `msclap/` subdirectory
from the pinned commit above and re-apply the modifications listed here.

## License

MIT-licensed by Microsoft. The full license text is consolidated into the
repository root `NOTICE` file alongside other vendored third-party code.
See "Modifications from upstream" above for the MA-side changes.
