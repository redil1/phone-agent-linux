# Kokoro runtime provenance

The files in this directory derive from `kokoro==0.9.4`, published by the
[hexgrad/kokoro](https://github.com/hexgrad/kokoro) project under Apache-2.0. The full licence is
preserved in `LICENSE`.

PhoneAgent modifications:

- package the runtime below PhoneAgent's namespace instead of installing the upstream distribution;
- remove process-global Loguru handler changes;
- pin every Hugging Face model and voice download to revision
  `f3ff3571791e39611d31c381e3a41a3af07b4987`;
- replace the in-process GPL phonemizer/loader dependency with `espeak-ng` executable calls in
  `espeak_subprocess.py`;
- keep the upstream Kokoro model API and segmentation behavior used by the qualified phone adapter.

The eSpeak NG executable is a separate GPL-3.0-or-later program supplied by the Linux distribution.
PhoneAgent does not import or link its library. Distribution images must preserve the package's GPL
notices and corresponding-source mechanism. This architectural separation is an engineering licence
control and still requires product-owner or counsel approval for the intended distribution.
