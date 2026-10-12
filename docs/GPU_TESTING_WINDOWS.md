# Testing NVIDIA GPU support on Windows

A step-by-step plan for a Windows PC with an NVIDIA card. It is written so a
Claude Code session on that PC can follow it end to end, starting from a fresh
clone of `github.com/VijaysinghPuwar/setuptts` (branch `main`) with Python
3.12 installed. Run the commands in PowerShell.

## How it works (read once)

- The app bundles the **CPU** build of onnxruntime 1.30.0. "GPU support" is an
  optional download, about 1.33 GB: onnxruntime-gpu 1.30.0 (cp312, win_amd64)
  plus NVIDIA's CUDA 13 and cuDNN 9 DLLs. It unpacks to about 2.9 GB in
  `%LOCALAPPDATA%\SetupTTSApp\SetupTTS\gpu_runtime\ort-1.30.0-cuda13\`.
- The setting `gpu_mode` has three values:
  - `"auto"` (the default) uses the card only when the speed check on this PC
    (`gpu_runtime\check.json`) found it at least 1.25× faster than the
    processor.
  - `"gpu"` always uses the card when GPU support is installed, and falls back
    to the processor if the card fails.
  - `"cpu"` never uses the card.

  In the UI these are the Automatic / Graphics card / Processor buttons under
  Settings → Graphics card → GPU Settings…. Each change takes effect only
  after the app restarts.
- At launch, `gpu_accel.startup(mode)` asks `decide()` whether to use the card
  and logs the answer as `Offline voices: graphics card (<why>)` or
  `Offline voices: processor (<why>)`. If it chooses the card, it installs an
  import hook so `import onnxruntime` loads the GPU build from the pack
  folder, and it adds the NVIDIA DLL folders to the DLL search path and
  `PATH`.
- `SetupTTS.exe --gpu-check out.json [--force] [--voice KEY]`, or
  `python main.py --gpu-check ...` in dev mode, runs the speed check in its own
  process. It times one offline voice on the processor and then on the card.
- If the card fails while a job is running, the job carries on using the
  processor (`PiperSynthesizer._fall_back_to_cpu`).

Paths. `AppPaths` uses platformdirs, so dev mode and the installed app use the
same folders unless `SETUPTTS_DATA_DIR` is set:

| What | Path |
|---|---|
| App data | `%LOCALAPPDATA%\SetupTTSApp\SetupTTS\` |
| GPU pack | `...\gpu_runtime\ort-1.30.0-cuda13\` |
| Speed-check result the app uses | `...\gpu_runtime\check.json` |
| Settings (`"gpu_mode"`: `auto` / `gpu` / `cpu`) | `...\settings.json` |
| Main log | `...\Logs\setuptts.log` |
| Speed-check log (rewritten each run) | `...\Logs\gpu-check.log` |

```powershell
$D = "$env:LOCALAPPDATA\SetupTTSApp\SetupTTS"   # used below
```

## 1. Prerequisites

```powershell
nvidia-smi
nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv
python --version        # must be 3.12.x (the GPU wheel is cp312)
```

The PC must meet all of these:

- Driver **580 or newer**, because CUDA 13 needs an R580 driver. If it is
  older, update it from nvidia.com or the NVIDIA app and run the check again.
- compute_cap **7.5 or higher**, which means an RTX 20-series, GTX 16-series or
  newer card. CUDA 13 dropped the GTX 900 and 10 series and the Titan V.
- At least **5 GB** of free space on the drive that holds `%LOCALAPPDATA%`.

Record the output of the second `nvidia-smi` line for the report.

## 2. Dev-mode setup and tests

```powershell
git clone https://github.com/VijaysinghPuwar/setuptts.git
cd setuptts
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt pytest pytest-qt
python scripts/fetch_bundled_voice.py
$env:QT_QPA_PLATFORM = "offscreen"; pytest -q; Remove-Item Env:QT_QPA_PLATFORM
```

All tests must pass. Do **not** `pip install onnxruntime-gpu` into the venv.
The point of the test is that the app loads the GPU build from the pack.

## 3. Download GPU support

**Option A, through the UI (preferred, because it also tests the dialog):**
run `python main.py`, then open Settings → Graphics card → **GPU Settings…** →
**Download**. Check that the progress bar moves and that cancelling and
downloading again resumes rather than starting over. When the download
finishes, the dialog runs the speed check by itself.

**Option B, with the script:**

```powershell
python scripts/gpu_pack_smoke.py
```

It prints progress every 10% and exits 0 once the pack is installed.

Verify the install:

```powershell
Get-Content "$D\gpu_runtime\ort-1.30.0-cuda13\.complete"
Get-ChildItem -Recurse "$D\gpu_runtime\ort-1.30.0-cuda13\nvidia" -Filter *.dll | Select-Object FullName
```

## 4. Speed check (dev mode)

```powershell
python main.py --gpu-check out.json
Get-Content out.json
Get-Content "$D\Logs\gpu-check.log"
```

Optionally, check a larger voice. It must be downloaded first through Get
Voices:

```powershell
python main.py --gpu-check out-ryan.json --voice en_US-ryan-high
```

How to read `out.json`:

| Field | Meaning / expected |
|---|---|
| `onnxruntime` | `"1.30.0"` |
| `providers_available` | Includes `CUDAExecutionProvider`. If it is missing, the GPU build was not loaded. |
| `gpu_name`, `driver`, `compute` | Your card, as nvidia-smi reports it |
| `cpu_chars_per_s` / `gpu_chars_per_s` | Characters of text spoken per second |
| `speedup` | gpu ÷ cpu |
| `ok` | The card ran the voice, and its audio length matched the processor's within 15% |
| `recommended` | `ok` **and** `speedup ≥ 1.25` (`gpu_accel.MIN_SPEEDUP`). Automatic mode uses the card only when this is true. |
| `error` / `detail` | A plain-language reason, plus a traceback when CUDA failed |

Expect a speedup of roughly **2–5×** on medium and high voices with an RTX
card. A very fast processor paired with an entry-level card can fall below
1.25×. That is a correct "not recommended", not a bug. Run the check 2–3 times
and report every result, because the first run includes CUDA warm-up.

Note: `check.json`, the file Automatic mode reads at launch, is written by the
GPU Settings dialog, not by `--gpu-check` itself. Use the dialog to run the
check before testing Automatic mode. If you copy `out.json` there by hand
instead, add `"pack": "ort-1.30.0-cuda13"` to the copy, or the app ignores the
file.

## 5. Standard benchmark (dev mode)

The reference text is the user's novel file `Simulating Endless
Possibilities_ Invincible Once I Act 201-300.txt`. It is **not in the repo**
because it is copyrighted. Ask the user to copy it to the PC, for example into
Downloads. Download the Ryan (high) voice through Get Voices first.

```powershell
$T = "$env:USERPROFILE\Downloads\Simulating Endless Possibilities_ Invincible Once I Act 201-300.txt"
python scripts/local_benchmark.py --text $T --device both          # quick: first 30k chars, CPU then GPU
python scripts/local_benchmark.py --text $T --device both --full   # optional: the whole book (long)
```

You can set `$env:SETUPTTS_BENCH_TEXT = $T` instead of passing `--text`. The
script runs the same path as a real job: Ryan (high), rate +24%, 1,200-char
chunks, and MP3 encoding. It prints chars/s for each device and the GPU/CPU
ratio, and it appends one JSON line per device to `benchmark_results.jsonl` in
the repo root. The output must say `reference` rather than
`NOT the reference text`. If it prints
`warning: the voice fell back to the processor`, the GPU run is invalid;
report it as a failure.

For comparison, a MacBook Pro M5 on the processor does about **140 chars/s**
on the full book.

## 6. The three modes in the app (dev mode)

Restart the app after every mode change, because the mode applies at launch.
Find the launch decision with:

```powershell
Select-String -Path "$D\Logs\setuptts.log" -Pattern "Offline voices:|GPU|graphics card" | Select-Object -Last 20
```

For each mode, convert the same long text (at least 20,000 characters, for
example the first chapters of the benchmark text) with **Ryan (high)**, and
time the job. While each job runs, watch the card in a second terminal:

```powershell
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv -l 1
nvidia-smi    # the process list at the bottom
```

| Mode | Expected log line | Expected during a job |
|---|---|---|
| **Automatic** on a strong RTX card, after the dialog's speed check | `Offline voices: graphics card (the graphics card was N.Nx faster)`, then `GPU support activated from ...` and `Offline voice en_US-ryan-high runs on the graphics card` | Utilisation well above idle, and SetupTTS in the process list. Faster than Processor mode. |
| **Automatic**, check not run or card slower | `Offline voices: processor (the speed check has not run yet / the processor was faster on this PC (card X.XXx) / ...)` | As in Processor mode |
| **Graphics card** | `Offline voices: graphics card (the graphics card was chosen in Settings)` | Same as Automatic on the card |
| **Processor** | `Offline voices: processor (the processor was chosen in Settings)`, and **no** `GPU support activated` | **No SetupTTS process** in `nvidia-smi` and about 0% utilisation. CPU load goes up in Task Manager instead. |

The audio should sound the same in every mode. Report the job time for each
mode.

## 7. Installed-release test

1. From https://github.com/VijaysinghPuwar/setuptts/releases/latest, download
   `SetupTTS-Windows-Installer.exe`, run it, and start SetupTTS. If the latest
   release has no Graphics card section in Settings, GPU support has not been
   released yet. In that case, use the `windows-installer-*` artifact from a
   manual run of the "Build Release" workflow (Actions tab, then Run
   workflow).
2. Repeat sections 3 and 6 with the installed app: Download, then the
   dialog's speed check, then all three modes with Ryan (high), restarting
   after each change and watching `nvidia-smi`.
3. Run the frozen app's own check. The app is windowed, so wait for it to
   finish:
   ```powershell
   $exe = "$env:LOCALAPPDATA\Programs\SetupTTS\SetupTTS.exe"
   Start-Process $exe -ArgumentList "--gpu-check","`"$pwd\out-frozen.json`"" -Wait
   Get-Content out-frozen.json; Get-Content "$D\Logs\gpu-check.log"
   ```
   The results should match dev mode. A failure that happens only in the
   frozen app points at packaging (see section 10).

## 8. Failure injection (the app must fall back, never crash)

Quit SetupTTS before each change.

1. **Broken install.** Set the mode to **Graphics card**, then rename a CUDA
   DLL in the pack:
   ```powershell
   $dll = Get-ChildItem -Recurse "$D\gpu_runtime\ort-1.30.0-cuda13\nvidia" -Filter cublasLt64_13.dll | Select-Object -First 1
   Rename-Item $dll.FullName "$($dll.Name).off"
   ```
   Start the app and convert with an offline voice. Expected results:
   - The job completes on the processor.
   - `setuptts.log` shows `Offline voices: graphics card (...)`, then
     `GPU support: cublasLt64_13.dll not found`, then
     `GPU stopped working (...); offline voices continue on the processor`.
   - There is no crash and no error dialog.

   Repeat with `cudnn64_9.dll`, which is in `nvidia\cudnn\bin`. Rename both
   files back afterwards.
2. **Automatic mode with a failed or slow check.** Edit
   `"$D\gpu_runtime\check.json"` to set `"speedup": 1.0`, set the mode to
   Automatic, and restart. The log should show
   `Offline voices: processor (the processor was faster on this PC (card 1.00x))`.
   Then set `"ok": false` and restart. The log should show
   `Offline voices: processor (the graphics card failed its speed check)`.
   Run the speed check again from the dialog to restore the file.
3. **Graphics card mode ignores the check.** With the edited `check.json`
   still in place, set the mode to Graphics card and restart. The log should
   show `Offline voices: graphics card (the graphics card was chosen in Settings)`
   and the job should run on the card.

## 9. Removal

In GPU Settings, click **Remove GPU support**. If the GPU was active in this
session, Windows won't delete the loaded DLLs. In that case the app writes
`gpu_runtime\.remove` and deletes the folder at the next start, logging
`Removed GPU support (scheduled at last session)`. Restart the app, then run:

```powershell
Test-Path "$D\gpu_runtime"     # expect False (or an empty folder)
```

The log should now show `Offline voices: processor (GPU support is not downloaded)`
in every mode except Processor, which logs its own reason. Offline voices
must work normally on the processor.

## 10. If something fails: where to look

- `app/services/gpu_accel.py`:
  - `decide()` maps the mode, the pack and `check.json` to a choice and a
    reason.
  - `startup()` logs the `Offline voices: ...` line.
  - `activate()` adds the DLL folders and `PATH`, sets
    `CUDA_DEVICE_ORDER=PCI_BUS_ID`, and installs the `_RedirectFinder` import
    hook. `startup()` then imports onnxruntime and undoes everything
    (`_deactivate()`) if that fails, so a damaged pack falls back to the
    bundled CPU build.
  - `ensure_cuda_loaded()` preloads `_PRELOAD_DLLS` by full path the first
    time a voice goes on the card (not at launch). `cuda_provider_loads()`
    (reported by `--gpu-check` as `cuda_provider_loads`) tells a missing DLL
    apart from "no device".
  - `requirement_problem()` checks the driver and compute capability.
  - `download_pack()` downloads and unpacks the pack.
- `app/services/piper_tts.py`:
  - `_open_voice(gpu=True)` passes the CUDA provider options from
    `gpu_accel.cuda_provider_options()`. These include
    `cudnn_conv_algo_search = HEURISTIC`; EXHAUSTIVE would re-benchmark every
    new sentence length and make long jobs slow. They also include
    `arena_extend_strategy = kSameAsRequested`.
  - `_open_voice` raises `GpuUnavailable` when onnxruntime silently dropped
    CUDA.
  - `_load()` and `_fall_back_to_cpu()` handle the switch to the processor.
- `app/gpu_check.py` is the speed check; `scripts/local_benchmark.py` is the
  standard benchmark.
- Common symptoms:
  - `Could not locate cudnn_*64_9.dll`, or error 126, in the logs: a cuDNN
    sub-library was not found through `PATH`.
  - `providers_available` without CUDA: the import hook lost to the bundled
    CPU build.
  - `ModuleNotFoundError` only in the frozen app: PyInstaller did not bundle
    a module that onnxruntime-gpu imports.
  - The card is slower than the processor: check that the provider options
    above were applied, and compare `gpu_chars_per_s` across several runs.

## 11. What to report back

1. The `nvidia-smi --query-gpu=...` line, the Windows version, the CPU model,
   and the SetupTTS version and source (dev checkout, or the installer from
   release X).
2. The `pytest -q` summary line.
3. The full `out.json` from every speed check: dev mode, the frozen app, and
   any `--voice` runs.
4. The new lines in `benchmark_results.jsonl`, quick and (if run) `--full`,
   copied verbatim.
5. Job times for the long text in each mode (Automatic, Graphics card,
   Processor), with the character count, the voice, and the peak
   `nvidia-smi` utilisation and memory. For Processor mode, confirm that no
   SetupTTS process appeared in `nvidia-smi`.
6. Log excerpts from:
   `Select-String -Path "$D\Logs\setuptts.log","$D\Logs\gpu-check.log" -Pattern "Offline voices:|GPU|graphics card|CUDA|cudnn|onnxruntime"`.
7. The result of each failure-injection and removal step, as pass or fail,
   with the matching log lines.
8. Any traceback, copied in full, from `detail` in the JSON, the logs, or a
   crash dialog.
