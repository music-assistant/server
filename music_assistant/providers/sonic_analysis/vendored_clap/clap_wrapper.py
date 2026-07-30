# ruff: noqa  # vendored code — see vendored_clap/README.md
from __future__ import annotations

import warnings
from pathlib import Path

# MA MOD: upstream applied warnings.filterwarnings("ignore") here at module scope,
# which would suppress warnings from all other MA providers for the rest of the
# process lifetime.  Removed; suppression is now scoped to load_clap() via a
# with warnings.catch_warnings() block.
import argparse
import collections
import math
import os
import random
import re
import sys

import numpy as np
import torch
import torchaudio.transforms as T
import yaml
from huggingface_hub.file_download import hf_hub_download
from transformers import AutoTokenizer, logging

from .models.clap import CLAP

logging.set_verbosity_error()


class CLAPWrapper:
    """
    A class for interfacing CLAP model.
    """

    # MA MOD: dropped "2022" and "clapcap" entries — Music Assistant only uses
    # the 2023 audio model. The captioning ("clapcap") variant required a
    # ~200-line mapper.py + caption-generation methods that would otherwise be
    # dead weight. Re-add from upstream's microsoft/CLAP if a future use case
    # needs them.
    model_repo = "microsoft/msclap"
    model_name = {
        "2023": "CLAP_weights_2023.pth",
    }

    # MA MOD: text_enabled=False skips the HF text-model + tokenizer downloads
    # entirely (~500MB GPT2 weights). Audio embedding still works; calling
    # get_text_embeddings on a text-disabled wrapper raises a clear RuntimeError.
    def __init__(
        self,
        model_fp: Path | str | None = None,
        version: str = "2023",
        use_cuda=False,
        *,
        text_enabled: bool = True,
    ):
        # Check if version is supported
        self.supported_versions = self.model_name.keys()
        if version not in self.supported_versions:
            raise ValueError(
                f"The version {version} is not supported. The supported versions are {self.supported_versions!s}"
            )

        self.text_enabled = text_enabled
        self.np_str_obj_array_pattern = re.compile(r"[SaUO]")
        self.file_path = os.path.realpath(__file__)
        self.default_collate_err_msg_format = (
            "default_collate: batch must contain tensors, numpy arrays, numbers, "
            "dicts or lists; found {}"
        )
        self.config_as_str = (Path(__file__).parent / f"configs/config_{version}.yml").read_text()

        # Automatically download model if not provided
        if not model_fp:
            model_fp = hf_hub_download(self.model_repo, self.model_name[version])

        self.model_fp = model_fp
        self.use_cuda = use_cuda
        # MA MOD: removed clapcap branch — only the 2023 audio model is supported.
        self.clap, self.tokenizer, self.args = self.load_clap()

    def read_config_as_args(self, config_path, args=None, is_config_str=False):
        return_dict = {}

        if config_path is not None:
            if is_config_str:
                yml_config = yaml.load(config_path, Loader=yaml.FullLoader)
            else:
                with open(config_path) as f:
                    yml_config = yaml.load(f, Loader=yaml.FullLoader)

            if args != None:
                for k, v in yml_config.items():
                    if k in args.__dict__:
                        args.__dict__[k] = v
                    else:
                        # MA MOD: stderr write never triggers because the vendored config_2023.yml
                        # is fixed — all its keys are known. Silenced to avoid bypassing MA logging.
                        pass  # sys.stderr.write(f"Ignored unknown parameter {k} in yaml.\n")
            else:
                for k, v in yml_config.items():
                    return_dict[k] = v

        args = args if args != None else return_dict
        return argparse.Namespace(**args)

    def load_clap(self):
        r"""Load CLAP model with args from config file"""
        args = self.read_config_as_args(self.config_as_str, is_config_str=True)

        if "roberta" in args.text_model or "clip" in args.text_model or "gpt" in args.text_model:
            self.token_keys = ["input_ids", "attention_mask"]
        elif "bert" in args.text_model:
            self.token_keys = ["input_ids", "token_type_ids", "attention_mask"]

        # MA MOD: scope CLAP/HuggingFace import-time warnings to the load only.
        # Upstream applied warnings.filterwarnings("ignore") at module scope which
        # would suppress warnings from all other MA providers for the rest of the
        # process lifetime.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")

            clap = CLAP(
                audioenc_name=args.audioenc_name,
                sample_rate=args.sampling_rate,
                window_size=args.window_size,
                hop_size=args.hop_size,
                mel_bins=args.mel_bins,
                fmin=args.fmin,
                fmax=args.fmax,
                classes_num=args.num_classes,
                out_emb=args.out_emb,
                text_model=args.text_model,
                transformer_embed_dim=args.transformer_embed_dim,
                d_proj=args.d_proj,
                skip_text_encoder=not self.text_enabled,  # MA MOD
            )

            # MA MOD: weights_only=False is required because the CLAP .pth stores
            # non-tensor metadata (the original args namespace). Source is trusted
            # (microsoft/msclap on HuggingFace Hub) so the unsafe-deserialization
            # risk that motivated CVE-2026-1839 doesn't apply here.
            model_state_dict = torch.load(self.model_fp, map_location=torch.device("cpu"))["model"]

            # We unwrap the DDP model and save. If the model is not unwrapped and saved, then the model needs to unwrapped before `load_state_dict`:
            # Reference link: https://discuss.pytorch.org/t/how-to-load-dataparallel-model-which-trained-using-multiple-gpus/146005
            clap.load_state_dict(model_state_dict, strict=False)

            clap.eval()  # set clap in eval mode
            # MA MOD: skip tokenizer download when text encoder is disabled.
            if self.text_enabled:
                tokenizer = AutoTokenizer.from_pretrained(args.text_model)
                if "gpt" in args.text_model:
                    tokenizer.add_special_tokens({"pad_token": "!"})
            else:
                tokenizer = None

            if self.use_cuda and torch.cuda.is_available():
                clap = clap.cuda()

        return clap, tokenizer, args

    # MA MOD: load_clapcap method removed — CLAP captioning model isn't used by MA.
    # Required get_clapcap from .models.mapper which is also removed in this drop.

    def default_collate(self, batch):
        r"""Puts each data field into a tensor with outer dimension batch size"""
        elem = batch[0]
        elem_type = type(elem)
        if isinstance(elem, torch.Tensor):
            out = None
            if torch.utils.data.get_worker_info() is not None:
                # If we're in a background process, concatenate directly into a
                # shared memory tensor to avoid an extra copy
                numel = sum([x.numel() for x in batch])
                storage = elem.storage()._new_shared(numel)
                out = elem.new(storage)
            return torch.stack(batch, 0, out=out)
        if (
            elem_type.__module__ == "numpy"
            and elem_type.__name__ != "str_"
            and elem_type.__name__ != "string_"
        ):
            if elem_type.__name__ == "ndarray" or elem_type.__name__ == "memmap":
                # array of string classes and object
                if self.np_str_obj_array_pattern.search(elem.dtype.str) is not None:
                    raise TypeError(self.default_collate_err_msg_format.format(elem.dtype))

                return self.default_collate([torch.as_tensor(b) for b in batch])
            if elem.shape == ():  # scalars
                return torch.as_tensor(batch)
        elif isinstance(elem, float):
            return torch.tensor(batch, dtype=torch.float64)
        elif isinstance(elem, int):
            return torch.tensor(batch)
        elif isinstance(elem, str):
            return batch
        elif isinstance(elem, collections.abc.Mapping):
            return {key: self.default_collate([d[key] for d in batch]) for key in elem}
        elif isinstance(elem, tuple) and hasattr(elem, "_fields"):  # namedtuple
            return elem_type(*(self.default_collate(samples) for samples in zip(*batch)))
        elif isinstance(elem, collections.abc.Sequence):
            # check to make sure that the elements in batch have consistent size
            it = iter(batch)
            elem_size = len(next(it))
            if not all(len(elem) == elem_size for elem in it):
                raise RuntimeError("each element in list of batch should be of equal size")
            transposed = zip(*batch)
            return [self.default_collate(samples) for samples in transposed]

        raise TypeError(self.default_collate_err_msg_format.format(elem_type))

    def read_audio(self, audio_path, resample=True):
        r"""Loads audio file or array and returns a torch tensor"""
        # MA MOD: swap torchaudio.load (which requires torchcodec + ffmpeg
        # shared libs on torch 2.11+) for librosa.load. Same input/output
        # contract: returns (channels, samples) tensor + sample rate.
        import librosa

        audio_np, sample_rate = librosa.load(audio_path, sr=None, mono=False)
        if audio_np.ndim == 1:
            audio_np = audio_np[None, :]  # (samples,) -> (1, samples)
        audio_time_series = torch.from_numpy(audio_np)

        resample_rate = self.args.sampling_rate
        if resample and resample_rate != sample_rate:
            resampler = T.Resample(sample_rate, resample_rate)
            audio_time_series = resampler(audio_time_series)
        return audio_time_series, resample_rate

    def load_audio_into_tensor(self, audio_path, audio_duration, resample=False):
        r"""Loads audio file and returns raw audio."""
        # Randomly sample a segment of audio_duration from the clip or pad to match duration
        audio_time_series, sample_rate = self.read_audio(audio_path, resample=resample)
        audio_time_series = audio_time_series.reshape(-1)

        # audio_time_series is shorter than predefined audio duration,
        # so audio_time_series is extended
        if audio_duration * sample_rate >= audio_time_series.shape[0]:
            repeat_factor = int(
                np.ceil((audio_duration * sample_rate) / audio_time_series.shape[0])
            )
            # Repeat audio_time_series by repeat_factor to match audio_duration
            audio_time_series = audio_time_series.repeat(repeat_factor)
            # remove excess part of audio_time_series
            audio_time_series = audio_time_series[0 : audio_duration * sample_rate]
        else:
            # audio_time_series is longer than predefined audio duration,
            # so audio_time_series is trimmed
            start_index = random.randrange(
                audio_time_series.shape[0] - audio_duration * sample_rate
            )
            audio_time_series = audio_time_series[
                start_index : start_index + audio_duration * sample_rate
            ]
        return torch.FloatTensor(audio_time_series)

    def preprocess_audio(self, audio_files, resample):
        r"""Load list of audio files and return raw audio"""
        audio_tensors = []
        for audio_file in audio_files:
            audio_tensor = self.load_audio_into_tensor(audio_file, self.args.duration, resample)
            audio_tensor = (
                audio_tensor.reshape(1, -1).cuda()
                if self.use_cuda and torch.cuda.is_available()
                else audio_tensor.reshape(1, -1)
            )
            audio_tensors.append(audio_tensor)
        return self.default_collate(audio_tensors)

    # MA MOD: tensor-in path so callers that already have audio loaded
    # (e.g. the merged sonic_analysis provider) can skip re-reading the
    # file from disk. Matches the pad/truncate semantics of
    # load_audio_into_tensor exactly.
    def preprocess_audio_from_tensor(self, audio_tensors_in, source_sample_rate):
        r"""Preprocess pre-loaded audio tensors (skips file I/O).

        :param audio_tensors_in: list of 1D torch.Tensor at source_sample_rate.
        :param source_sample_rate: Sample rate of the input tensors (int).
        """
        target_sr = self.args.sampling_rate
        out = []
        for audio_ts in audio_tensors_in:
            ts = audio_ts
            if ts.dim() == 2:
                ts = ts.mean(dim=0)
            if source_sample_rate != target_sr:
                resampler = T.Resample(source_sample_rate, target_sr)
                ts = resampler(ts)
            ts = ts.reshape(-1).to(torch.float32)
            duration_samples = int(self.args.duration * target_sr)
            if duration_samples >= ts.shape[0]:
                repeat_factor = int(math.ceil(duration_samples / ts.shape[0]))
                ts = ts.repeat(repeat_factor)[:duration_samples]
            else:
                start_index = random.randrange(ts.shape[0] - duration_samples)
                ts = ts[start_index : start_index + duration_samples]
            ts = ts.reshape(1, -1)
            if self.use_cuda and torch.cuda.is_available():
                ts = ts.cuda()
            out.append(ts)
        return self.default_collate(out)

    def get_audio_embeddings_from_tensor(self, audio_tensors_in, source_sample_rate):
        r"""Audio embedding path for pre-loaded tensors. Mirrors get_audio_embeddings."""
        preprocessed = self.preprocess_audio_from_tensor(audio_tensors_in, source_sample_rate)
        return self._get_audio_embeddings(preprocessed)

    def preprocess_text(self, text_queries):
        r"""Load list of class labels and return tokenized text"""
        # MA MOD: clear error when caller forgot the text encoder is disabled.
        if not getattr(self, "text_enabled", True):
            raise RuntimeError(
                "CLAP text encoder is disabled (text_enabled=False); "
                "construct CLAPWrapper(..., text_enabled=True) to enable text queries."
            )
        tokenized_texts = []
        for ttext in text_queries:
            if "gpt" in self.args.text_model:
                ttext = ttext + " <|endoftext|>"
            # MA MOD: tokenizer.encode_plus() was removed in transformers 5.x;
            # __call__ is the v5 replacement with identical kwargs/return type.
            tok = self.tokenizer(
                ttext,
                add_special_tokens=True,
                max_length=self.args.text_len,
                padding="max_length",
                return_tensors="pt",
            )
            for key in self.token_keys:
                tok[key] = (
                    tok[key].reshape(-1).cuda()
                    if self.use_cuda and torch.cuda.is_available()
                    else tok[key].reshape(-1)
                )
            tokenized_texts.append(tok)
        return self.default_collate(tokenized_texts)

    def get_text_embeddings(self, class_labels):
        r"""Load list of class labels and return text embeddings"""
        preprocessed_text = self.preprocess_text(class_labels)
        return self._get_text_embeddings(preprocessed_text)

    def get_audio_embeddings(self, audio_files, resample=True):
        r"""Load list of audio files and return a audio embeddings"""
        preprocessed_audio = self.preprocess_audio(audio_files, resample)
        return self._get_audio_embeddings(preprocessed_audio)

    def _get_text_embeddings(self, preprocessed_text):
        r"""Load preprocessed text and return text embeddings"""
        with torch.no_grad():
            return self.clap.caption_encoder(preprocessed_text)

    def _get_audio_embeddings(self, preprocessed_audio):
        r"""Load preprocessed audio and return a audio embeddings"""
        with torch.no_grad():
            preprocessed_audio = preprocessed_audio.reshape(
                preprocessed_audio.shape[0], preprocessed_audio.shape[2]
            )
            # Append [0] the audio emebdding, [1] has output class probabilities
            return self.clap.audio_encoder(preprocessed_audio)[0]

    def _generic_batch_inference(self, func, *args):
        r"""Process audio and/or text per batch"""
        input_tmp = args[0]
        batch_size = args[-1]
        # args[0] has audio_files, args[1] has class_labels
        inputs = [args[0], args[1]] if len(args) == 3 else [args[0]]
        args0_len = len(args[0])
        # compute text_embeddings once for all the audio_files batches
        if len(inputs) == 2:
            text_embeddings = self.get_text_embeddings(args[1])
            inputs = [args[0], args[1], text_embeddings]
        dataset_idx = 0
        for _ in range(math.ceil(args0_len / batch_size)):
            next_batch_idx = dataset_idx + batch_size
            # batch size is bigger than available audio/text items
            if next_batch_idx >= args0_len:
                inputs[0] = input_tmp[dataset_idx:]
                yield func(*tuple(inputs))
            else:
                inputs[0] = input_tmp[dataset_idx:next_batch_idx]
                yield func(*tuple(inputs))
            dataset_idx = next_batch_idx

    def get_audio_embeddings_per_batch(self, audio_files, batch_size):
        r"""Load preprocessed audio and return a audio embeddings per batch"""
        return self._generic_batch_inference(self.get_audio_embeddings, audio_files, batch_size)

    def get_text_embeddings_per_batch(self, class_labels, batch_size):
        r"""Load preprocessed text and return text embeddings per batch"""
        return self._generic_batch_inference(self.get_text_embeddings, class_labels, batch_size)

    def compute_similarity(self, audio_embeddings, text_embeddings):
        r"""Compute similarity between text and audio embeddings"""
        audio_embeddings = audio_embeddings / torch.norm(audio_embeddings, dim=-1, keepdim=True)
        text_embeddings = text_embeddings / torch.norm(text_embeddings, dim=-1, keepdim=True)

        logit_scale = self.clap.logit_scale.exp()
        similarity = logit_scale * text_embeddings @ audio_embeddings.T
        return similarity.T

    def classify_audio_files_per_batch(self, audio_files, class_labels, batch_size):
        r"""Compute classification probabilities for each audio recording in a batch and each class label"""
        return self._generic_batch_inference(
            self.classify_audio_files, audio_files, class_labels, batch_size
        )

    # MA MOD: generate_caption() and _generate_beam() removed — Music Assistant
    # uses the audio embeddings only, never the captioning model. Beam-search
    # caption generation depended on self.clapcap which is no longer
    # constructed. Re-add from upstream's microsoft/CLAP if a future use case
    # needs caption generation.
