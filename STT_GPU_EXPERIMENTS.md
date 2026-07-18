# STT/GPU experiment record

## Purpose

Select English streaming speech-to-text that equals or exceeds Groq Whisper
Large V3 Turbo on Will's voice, rejects household silence/noise, and fits beside
Alan Home TTS on Lovelace's RTX 2070 SUPER.

## Controlled corpus

The 24-trial ground-truth study was recorded on Boltzmann on 2026-07-15:

`~/.local/state/agent-fleet/voice-study/20260715-102039`

The comparison copy on Lovelace is currently:

`/tmp/alan-voice-study-20260715-102039`

Trials 1–8 are controls and Alan commands, 9–14 technical/domain language,
15–16 deliberate pauses, 17–22 natural dictation, 23 keyboard noise, and 24
room silence. `prompts.jsonl` is the ground truth. The study harness and its
automatic speech boundary recording were committed in `accd947`, `2650be5`
and `82b0580`. Raw household audio remains local; do not commit or upload it.

All local streaming runs used Alan Home's real `StreamingSession`, 4096-sample
blocks, RNNT decoding and its text-stability endpointing. Speaker embedding was
omitted from the benchmark event only. WER is lowercase word-level Levenshtein
distance after tokenizing on non-alphanumeric characters, with trials 1–22
aggregated; the corpus is
small, so group results and individual command failures matter alongside WER.

## Models tested

| System | Lookahead | WER | Natural | Domain | Negative trials |
|---|---:|---:|---:|---:|---|
| Nemotron native mixed FP16 (FP16 encoder, FP32 RNNT) | 1120 ms | 7.7% | 4.1% | 13.2% | both empty |
| Nemotron Speech Streaming English 0.6B native FP32 | 1120 ms | 7.7% | 4.1% | 13.2% | both empty |
| Nemotron Speech Streaming English 0.6B native FP32 | 560 ms | 9.3% | 4.7% | 17.6% | both empty |
| Groq Whisper Large V3 Turbo | remote | 11.2% | 8.8% | 14.7% | hallucinated on both |
| Original Alan Home FastConformer 114M | 480 ms | 17.4% | 8.1% | 32.4% | both empty |
| Original Alan Home FastConformer 114M | 1040 ms | 18.1% | 5.4% | 38.2% | both empty |

Groq returned `KRYV` repeatedly for keyboard noise and `Kymys, Tmux, Tmux` for
room silence. All local NVIDIA runs returned no text. Nemotron preserved a spoken
self-correction which Groq partly cleaned away. Nemotron still missed `pause`
once and made specialist-name errors, so command recognition needs its own
acceptance test rather than relying on aggregate WER.

The reduced-precision 1120 ms run produced exactly the same 24 transcripts as
FP32: controls 14.3% WER, domain 13.2%, deliberate pauses 9.1%, natural
dictation 4.1%, and 7.7% overall. Direct whole-model `.half()` is not supported
by this NeMo streaming path: the FP32 preprocessor first failed against an FP16
encoder convolution (`Input type (float) and bias type (c10::Half) should be the
same`), and autocast then exposed the RNNT CUDA-graph boundary (`mat1 and mat2
must have the same dtype, but got Float and Half`). The successful native run
therefore keeps the RNNT decoder and joint FP32 while running the 600M encoder
FP16 under autocast.

Result files on Lovelace:

- `/tmp/alan-voice-study-20260715-102039/groq.jsonl`
- `/tmp/alan-voice-study-20260715-102039/nemotron-native-560.jsonl`
- `/tmp/alan-voice-study-20260715-102039/nemotron-native-1120.jsonl`
- `/tmp/alan-voice-study-20260715-102039/nemotron-native-fp16-1120.jsonl`
- `/tmp/alan-voice-study-20260715-102039/nemotron-native-fp16-1120-summary.json`
- `/tmp/alan-voice-study-20260715-102039/nemotron-native-fp16-1120-score.json`
- `/tmp/alan-voice-study-20260715-102039/fastconformer-480.jsonl`
- `/tmp/alan-voice-study-20260715-102039/fastconformer-1040.jsonl`

## GPU measurements

Lovelace GPU 1 is an RTX 2070 SUPER with 8192 MiB nominal and about 7792 MiB
reported free when empty.

| Component | Peak/reserved GPU memory |
|---|---:|
| Alan Home TTS | approximately 3324 MiB observed settled footprint |
| Original FastConformer 480 | 1014 MiB peak reserved |
| Original FastConformer 1040 | 1110 MiB peak reserved |
| Nemotron English 0.6B FP32 1120 | 4926 MiB peak reserved |
| Nemotron English 0.6B mixed FP16 1120 | 1308 MiB peak allocated; 1388 MiB peak reserved |

The Nemotron 560 and 1120 settings use the same weights; lookahead changes
latency and accuracy, not meaningful model memory. Full-precision Nemotron plus
the observed TTS footprint totals about 8250 MiB before additional overhead and
therefore does not fit safely. The original model plus TTS fits comfortably.
The mixed-FP16 benchmark's final allocation was 1259 MiB and its peak was 1308
MiB, with 1388 MiB reserved at peak. Adding that reserved figure to the
separately observed TTS process footprint gives approximately 4712 MiB, but
this is only a plausibility check: allocator and `nvidia-smi` measurements are
not identical, and coexistence still requires one simultaneous test.
The 560 ms preliminary ONNX INT8 run was on Boltzmann CPU and consumed no
Lovelace GPU memory; it is not comparable to native FP32 memory.

Timing over pre-recorded files, not perceived streaming latency: Groq median API
time 0.206 s; native Nemotron 560 median compute 0.321 s; native Nemotron 1120
0.180 s; mixed-FP16 Nemotron 1120 median compute 0.237 s (6.182 s total over 24
files, after a 5.238 s load). The 1120 configuration processes fewer chunks but
has 1.12 seconds of algorithmic lookahead, versus 0.56 seconds for 560.

## Model facts

- Original Alan Home checkpoint:
  `stt_en_fastconformer_hybrid_large_streaming_multi`, approximately 114M
  parameters, hybrid RNNT/CTC, released in 2023.
- Current candidate:
  `nvidia/nemotron-speech-streaming-en-0.6b`, 600M parameters, cache-aware
  FastConformer RNNT, March 2026 English checkpoint, stored as FP32.
- Nemotron 3.5 is multilingual. NVIDIA recommends the English checkpoint for an
  English-only application, so 3.5 has not been benchmarked merely for breadth.
- NVIDIA publishes no official quantized checkpoint. The preliminary INT8 ONNX
  conversion is community-produced and must be treated as a separate runtime.

## Current service state

Both `alan-home-stt.service` and `alan-home-tts.service` were deliberately left
stopped during investigation. Do not start, stop or cycle either service merely
to diagnose a model-load failure. The earlier native-load failure was an API
mismatch (`change_decoding_strategy(decoder_type=...)` on an RNNT-only model),
not evidence of GPU exhaustion.

## Unfinished experiments

1. Run an explicit mixed-FP16 Nemotron/TTS coexistence test. Do not infer
   coexistence from adding two independently measured numbers.
2. Run the existing INT8 ONNX English 560 and 1120 variants over all 24 files,
   recording CPU model, thread count, real-time factor, WER and negative trials.
3. Compare live subjective latency of the best 560 and 1120 candidates only
   after fidelity and memory gates pass.
4. Build a dedicated Alan command set (`pause`, `resume`, `send`, `cancel` and
   near-homophones) because aggregate dictation WER does not establish command
   reliability.
5. Restore production STT/TTS only after the selected configuration and service
   ownership are explicit.

## Decision rule

The target is Groq-level or better practical fidelity, empty output on negative
audio, and measured coexistence with TTS. Mixed-FP16 Nemotron 1120 now passes
the corpus fidelity and silence gates and has a plausible independent memory
footprint. FastConformer passes memory and silence gates but fails fidelity.
The remaining gate is measured simultaneous coexistence with TTS; neither
service should be restored merely from the arithmetic estimate.
