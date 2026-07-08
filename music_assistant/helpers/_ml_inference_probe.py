"""
Out-of-process probe that verifies the CPU can execute on-device ML inference.

Run as a short-lived subprocess by ``helpers.util.verify_cpu_supports_ml_inference()``; the
process exit code carries the verdict. Running the inference here, in a throwaway process,
means a CPU that faults on the vector instructions torch emits (common on virtual machines
that do not pass the host CPU through) crashes only this probe -- never the server.
"""

from __future__ import annotations

# Exit codes interpreted by the parent. A clean "no AVX2" rejection is distinct from a
# native crash (which the parent sees as a negative, signal-valued return code) and from
# any other exit (which the parent treats as inconclusive and lets through).
PROBE_CAPABLE = 0
PROBE_NO_AVX2 = 10


def run_probe() -> int:
    """Run representative inference kernels and return the verdict exit code."""
    import torch  # noqa: PLC0415

    if torch.backends.cpu.get_cpu_capability() in ("DEFAULT", "NO AVX"):
        return PROBE_NO_AVX2

    # Exercise the kernel families the analysis providers depend on -- a BLAS matmul, an
    # FFT, a convolution and an fbgemm-quantized matmul (the path most likely to use an
    # instruction a VM masks) -- on tiny tensors. The cost is the torch import, not the math.
    torch.set_num_threads(1)
    # Prefer fbgemm -- the x86 quantized backend the analysis models use and the one most
    # likely to hit a masked instruction -- and fall back to qnnpack on ARM.
    for engine in ("fbgemm", "qnnpack"):
        if engine in torch.backends.quantized.supported_engines:
            torch.backends.quantized.engine = engine
            break
    with torch.inference_mode():
        matrix = torch.randn(64, 64)
        torch.mm(matrix, matrix)
        torch.stft(torch.randn(2048), n_fft=512, window=torch.hann_window(512), return_complex=True)
        conv = torch.nn.Conv1d(1, 4, kernel_size=8)
        conv(torch.randn(1, 1, 2048))
        linear = torch.nn.Sequential(torch.nn.Linear(64, 64))
        quantized = torch.ao.quantization.quantize_dynamic(  # type: ignore[no-untyped-call]
            linear, {torch.nn.Linear}, dtype=torch.qint8
        )
        quantized(torch.randn(8, 64))
    return PROBE_CAPABLE


if __name__ == "__main__":
    raise SystemExit(run_probe())
