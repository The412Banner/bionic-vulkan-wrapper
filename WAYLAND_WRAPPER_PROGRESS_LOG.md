# Wayland wrapper -- progress log

Goal: a CI-built `libvulkan_wrapper.so` (leegao bionic Vulkan wrapper, this fork) with the X11 **and**
Wayland WSI, so a game on Bannerlator's Wayland backend runs on any community Android Turnip loaded
underneath through AdrenoTools, with one driver pick. Route A: rebuild the existing wrapper; no thin
shim yet. Artifacts only -- no releases, tags or catalog changes.

Branch `banner/wayland-wsi`, base = leegao `wrapper` @ `c8baafb` (Mesa 24.2.5).
Build: `banner/build_wayland_wrapper.sh`, workflow `.github/workflows/banner-wayland-wrapper.yml`.

## 2026-09-28

- Forked leegao/bionic-vulkan-wrapper. Recipe taken from leegao/vulkan_wrapper_termux-packages
  (`packages/vulkan-wrapper-android/build.sh`); its `leegao.patch` is a stale snapshot that no longer
  applies to the branch, so the build uses the branch as is.
- Changes: meson.build no longer defines HAVE_WL_DISPATCH_QUEUE_TIMEOUT / HAVE_WL_CREATE_QUEUE_WITH_NAME
  (old-libwayland compat; this Mesa has no wl_fixes). platforms = x11,wayland. Static libc++,
  NEEDED libandroid-shmem renamed to libandroid-sysvshm (as shipped), RUNPATH $ORIGIN,
  libadrenotools.so built from leegao/libadrenotools for linking only.
- banner_ahb_wsi.py (zero-copy) does NOT apply as is: its anchors are current Mesa main
  (first miss: `#include "color-management-v1-client-protocol.h"`). Plain Wayland WSI first.
- CI run 1: started (see below).
- Run 1 `36471823104` (90fee58): FAILURE at libadrenotools link -- its CMakeLists links `android`
  but not `log` (`__android_log_print` undefined). SPIRV-Tools static libs built fine (committed
  headers match the fork). Fix: `-DCMAKE_SHARED_LINKER_FLAGS=-llog`.
- Run 2: started.
- Run 2 `36472278631` (00f1753): FAILURE in the build script's own check, not the build:
  `readelf | grep -q` under `pipefail` (grep -q exits early, readelf takes SIGPIPE). libadrenotools
  linked fine. Fix: every `| grep -q` check now reads a here-string.
- Added the `ahb` matrix leg: `banner/ahb/banner_ahb_wsi_mesa242.py` = Banners-Turnip
  `patches/wayland/banner_ahb_wsi.py` @ 0a6846d with this Mesa's anchors (helpers unchanged; no
  color-management / loader_wayland_wrap_buffer here, unbraced `continue` in the format loops).
  Dry-applied cleanly to the tree; compile untested until CI. `plain` stays the primary deliverable.
- Run 3: started.
- Run 3 `36472845871` (86e6e87): both legs FAILURE at compile; meson configure passed with
  x11,wayland. (1) wrapper_device_memory.c uses O_RDWR/O_CLOEXEC without <fcntl.h> (Termux's
  headers pulled it in). (2) the trampoline generator treats every non-const pointer param as an
  output and dereferences it for logging; it exempted Display / xcb_connection_t but not
  wl_display (vkGetPhysicalDeviceWaylandPresentationSupportKHR). The ahb leg's
  wsi_common_wayland.c and banner-ahb-v1-protocol.c COMPILED.
- Run 4: started.
- Run 4 `36473622149` (9962585): FAILURE, same class: wrapper_physical_device.c calls open()/O_RDONLY
  without <fcntl.h> (the only other wrapper file doing so). Fixed.
- Run 5: started.
- Run 5 `36474321586` (219c006): COMPILED AND LINKED (both legs). Failed the checks:
  `vk_icdNegotiateLoaderICDInterfaceVersion` / `vk_icdGetPhysicalDeviceProcAddr` hidden by my
  `--exclude-libs,ALL` (they live in the static vulkan runtime); NEEDED had libcutils.so / libsync.so
  (android_stub stubs: util/os_misc property_get + atrace on DETECT_OS_ANDROID) and Termux's
  libz.so.1 / libzstd.so.1. The shipped wrapper imports none of those: it was built with Mesa's
  Android detection off (Termux mesa 0000-disable-android-detection.patch).
  Fixes: --exclude-libs only for libc++/libc++abi/libunwind/SPIRV-Tools archives; DETECT_OS_ANDROID
  and vk_android_native_buffer.h's Android branch off under __TERMUX__; zlib = NDK system libz;
  -Dzstd=disabled. Checks now collect all failures and the artifacts upload even on failure.
- Run 6: started.
- Run 6 `36475342083` (f2e7c55): FAILURE compiling wsi_common_x11.c: its __TERMUX__ AHB path needs
  native_handle_t, which came through vk_android_native_buffer.h's Android branch. Reverted that
  header to leegao's (header-only, no link effect); DETECT_OS_ANDROID stays off.
- Run 7: started.
- Run 7 `36476148938` (2fd6050): SUCCESS, both legs, all checks green (NEEDED all in imagefs/system/
  Proton lib; 3 vk_icd exports; 28 wl_* imports, none of the >=1.23 ones; ahb leg: banner_ahb_v1 in).
  NOT staged: review of the artifact found (a) the .so is unstripped (102 MB; -Dstrip only applies
  on install), (b) an unversioned `__emutls_get_address` import (Mesa's thread_local qsort_r
  fallback, emulated TLS at API 26) that nothing on the device exports -- with BIND_NOW that would
  fail the dlopen, (c) `memfd_create` imported non-weak (shipped: weak @LIBC_R).
  Fixes: llvm-strip --strip-unneeded; -fno-emulated-tls; -D__ANDROID_UNAVAILABLE_SYMBOLS_ARE_WEAK__;
  checks for both imports.
- Run 8: started.
- Run 8 `36477429842` (e5b6f97): SUCCESS, stripped (9.8 MB), no emulated TLS -- but the weak-symbols
  macro made meson detect qsort_r and pthread_{get,set}affinity_np (API 36) as available, so the .so
  imported them weak: a null call on anything older than Android 16. NOT staged.
  Fix: macro dropped (API-26-correct detection), checks fail on those three imports; memfd_create is
  logged (non-weak = Android 11+, the shipped one has it weak) with a diagnostic of which archive
  references it.
- Run 9: started.
- Run 9 `36478317956` (da89209): SUCCESS, both legs, headSha verified. Stripped 9.8 MB .so; NEEDED
  libandroid-sysvshm libadrenotools liblog libnativewindow libz libm libxcb libX11-xcb libxcb-dri3
  libxcb-present libxcb-sync libxcb-randr libxcb-shm libwayland-client libdrm libdl libc (all in
  imagefs/usr/lib, the system, or the Proton wcp's lib/); exports exactly the 3 vk_icd* entry points;
  28 wl_* imports, all exported by the libwayland-client in Proton 11.0-7-arm64ec-8 and
  11.0-2.1-arm64ec-16 on the device, none of the >=1.23 symbols; no emutls/qsort_r/affinity imports;
  memfd_create weak (as shipped); ELF TLS (Android 10+; device is API 34). ahb leg: banner_ahb_v1 in.
- STAGED to /sdcard/Download/Wayland/:
  - Wrapper-Wayland-TEST-da89209.zip      sha256 b561054f82c5b9c6a130b5eb4a112f6b6161ba354257ed084b2fde373691b1fa
  - Wrapper-Wayland-AHB-TEST-da89209.zip  sha256 1aadfbde2b0569133b626ea550cc8753455dfb9c5dbc0e1d9a32254dd5484216
  - Wrapper-Wayland-da89209-raw/ (libvulkan_wrapper.so dd5b7c4e..., libvulkan_wrapper-ahb.so 70462c1f...,
    wrapper_icd.aarch64.json)
- Device status: CI-green only, NOT device-tested.

## 2026-09-28 (later) -- device test of da89209 (plain)

- Coordinator's device test (Pocket FIT, Adreno 750; DiRT Showdown, Force Wayland, AdrenoTools
  "Mesa Turnip v26.3.0-20260830-r4", Proton 11.0-2.1-arm64ec-16): black screen, exit after ~0.7 s.
  The wrapper loaded, AdrenoTools loaded the Turnip, DXVK 2.4.1 enumerated "Adreno (TM) 750",
  then `vkCreateDevice Exception 0xc0000005 in Unix call`. 0 GPU frames reached the compositor.
- Key finding: the X11 wrapper Bannerlator SHIPS (imagefs usr/lib/libvulkan_wrapper.so) is NOT
  leegao's wrapper. Its strings ("Wrapper(%s)" device-name prefix, ../src/vulkan/wsi/wsi_common_android.c,
  ../src/vulkan/wrapper/spirv_patcher.cpp, WRAPPER_SAFE_CREATE_DEVICE, WRAPPER_DMAHEAP_CACHED,
  WRAPPER_DRIVER_ID) match Pipetto-crypto/mesa branch wrapper-25 (Mesa 25.0; GameNative/mesa is a
  fork of it). leegao's tree has none of those strings and prints the "non-dxvk game engines"
  warning seen in the device log -- so da89209 was a different wrapper than the one that runs this
  game on X11. That is also why DXVK showed no "Wrapper(" prefix.
- The leegao vkCreateDevice fault itself is not root-caused (no unix backtrace yet). Candidate spots
  in leegao's WRAPPER_CreateDevice: the BCn InterceptorState_Init compute pipelines it builds inside
  vkCreateDevice (Pipetto has none), and the extension list it forces onto the Turnip
  (wrapper_append_required_extensions).
- New: SOURCE=pipetto mode in banner/build_wayland_wrapper.sh + a `pipetto` matrix leg: clones
  Pipetto-crypto/mesa @ ecdd0da (wrapper-25), applies the same two changes (old-libwayland compat,
  Android detection off under __TERMUX__), -include fcntl.h, libadrenotools from its own subproject,
  same checks. leegao legs kept for reference.
- Run 10: started.
- Run 10 `36485571348` (154d0c6): leegao legs green; **pipetto leg compiled and linked** (NEEDED
  libandroid-sysvshm libadrenotools libnativewindow libm libxcb libX11-xcb libxcb-dri3/present/sync/
  randr/shm libwayland-client libdrm libdl libc; 3 vk_icd exports) but failed one check: it imports
  `__emutls_get_address` despite -fno-emulated-tls. Run 11 adds a diagnostic naming the object /
  archive that references it.
- Run 11 `36486345016` (4ca7414): FAILURE, same single check. The diagnostic found NO reference to
  `__emutls_get_address` in any wrapper object or any static archive in the build (Mesa libs, the
  expat / libadrenotools subprojects), so it comes from the NDK's static C++ runtime pieces the
  Pipetto C++ code pulls in (built with emulated TLS). The only thing on the device that exports it
  is libc++_shared.so -- which is exactly what the shipped (Pipetto) wrapper links.
  Fix: pipetto leg links libc++_shared.so (NEEDED, like shipped); new check: every C++-runtime
  symbol the .so imports must be in banner/imagefs-libc++_shared.exports.txt (export list of the
  device's imagefs libc++_shared.so, 2336 symbols, includes __emutls_get_address); plus a check for
  the "Wrapper(%s)" device-name format. Zip renamed Wrapper-Wayland-PIPETTO-TEST-<sha>.zip.
- Run 12: started.
- Run 12 `36491296974` (a149ed1): pipetto leg built with NEEDED libc++_shared.so; failed only my new
  check, which wrongly counted bionic's `__cxa_atexit` / `__cxa_finalize` (@LIBC) as C++-runtime
  imports. All 18 real libc++ imports are in the imagefs libc++_shared.so. Check fixed (skip @LIBC).
- Run 13: started.
- Run 13 `36491916124` (1e397c3): SUCCESS on all three legs, headSha verified. Pipetto leg:
  2.6 MB stripped .so, SONAME libvulkan_wrapper.so, RUNPATH $ORIGIN, BIND_NOW; NEEDED
  libandroid-sysvshm libadrenotools libnativewindow libm libxcb libX11-xcb libxcb-dri3/present/sync/
  randr/shm libwayland-client libdrm libc++_shared libdl libc (same set as the shipped X11 wrapper +
  libwayland-client, minus nothing); exports only the 3 vk_icd* entry points; 29 wl_* imports, all
  exported by the Proton 11.0-2.1-arm64ec-16 / 11.0-7-arm64ec-8 libwayland-client, none of the
  >=1.23 ones; 18 C++ runtime imports, all in the imagefs libc++_shared.so; memfd_create weak (as
  shipped); "Wrapper(%s)" device-name format present -> DXVK will show "Wrapper(Adreno (TM) 750)";
  VK_KHR_wayland_surface + VK_KHR_xcb_surface present.
- STAGED /sdcard/Download/Wayland/Wrapper-Wayland-PIPETTO-TEST-1e397c3.zip
  sha256 cd2f06f535bf1419832344c0574bf61c43733620750dce48f45fe6be8bb4afe3
  (+ Wrapper-Wayland-PIPETTO-1e397c3-raw/libvulkan_wrapper.so 527b81c1...). CI-green, not device-tested.

## 2026-09-28 18:30 -- device test of PIPETTO 1e397c3

- vkCreateDevice passes; DXVK: "Device : Wrapper(Adreno (TM) 750)", swapchain created, the game
  renders (compositor counts ~756 GPU frames/10 s) but the screen is WHITE: compositor log
  `could not import GPU frames ... (1280x720, modifier 0xffffffffffffff)` = DRM_FORMAT_MOD_INVALID.
- Cause: Pipetto src/vulkan/wrapper/wrapper_physical_device.c:213-222 calls wsi_device_init() and
  never sets `wsi_device.supports_modifiers` (real drivers set it themselves after init; Turnip does).
  So src/vulkan/wsi/wsi_common_wayland.c:3009 (`if (display->wl_dmabuf && wsi_device->supports_modifiers)`)
  skips the compositor's modifier list, wsi_common_drm.c:513 falls back to the legacy "scanout"
  image, and the dma-buf goes out with an implicit modifier.
- Fix (applied to the Pipetto tree by the build script, anchors asserted):
  supports_modifiers = driver has VK_EXT_image_drm_format_modifier && VK_EXT_external_memory_dma_buf
  (BANNER_WSI_NO_MODIFIERS=1 = old behaviour), logged once as "wrapper-wsi: explicit DRM format
  modifiers on|off"; every Wayland swapchain logs "wrapper-wsi: wayland swapchain WxH ... modifier 0x..."
  (stderr -> wine_debug.log). The __TERMUX__ X11 path presents AHardwareBuffers and never used
  modifiers; its DRI3 modifier re-query is kept off so X11 behaviour does not change.
- Run 14: started.
- Secondary: banner/ahb/banner_ahb_wsi_mesa242.py dry-applies cleanly to the Pipetto tree's Mesa 25.0
  WSI too, so a `pipetto-ahb` leg (Pipetto + explicit modifiers + banner_ahb_v1 zero-copy) is added.
- Run 15 (adds pipetto-ahb; run 14 still going): started.
- Run 14 `36493221427` (8efa5b3): SUCCESS (pipetto, plain, ahb).
- Run 15 `36493298477` (24acc19): SUCCESS on all four legs incl. pipetto-ahb (banner_ahb_v1 in).
  Same .so facts as run 13 (NEEDED set, 3 vk_icd exports, wl_* imports all in the Proton
  libwayland-client, 18 C++ imports all in imagefs libc++_shared, "Wrapper(%s)") plus the two
  "wrapper-wsi:" log lines.
- STAGED /sdcard/Download/Wayland/:
  - Wrapper-Wayland-PIPETTO-TEST-24acc19.zip      sha256 3a57b31e2baa4630378c3cacd55b35df12fee80a07e6872deeddeb01058d65c3
  - Wrapper-Wayland-PIPETTO-AHB-TEST-24acc19.zip  sha256 20a1131a1032ba804194e5fef212e9fceebaa575e88e1997414e4d5989ad4c51
  CI-green, not device-tested.

## 2026-09-28 19:00 -- DEVICE-PROVEN: DiRT Showdown on Wayland through the Pipetto wrapper (24acc19)

- Device: AYANEO Pocket FIT (Adreno 750), Bannerlator 3.1.3, container 3, Proton 11.0-2.1-arm64ec-16, Force Wayland.
  The only real driver pick: AdrenoTools "Mesa Turnip v26.3.0-20260830-r4" (compositor + underneath the wrapper).
- AHB build (Wrapper-Wayland-PIPETTO-AHB-TEST-24acc19) + BANNER_WAYLAND_ZERO_COPY=1: DXVK "Wrapper(Adreno (TM) 750)",
  `banner-ahb: 1280x720 swapchain (5 images) on gralloc buffers: UBWC (QCOM_COMPRESSED)`; menu at 144 fps (display cap),
  1438/1438 zero-copy frames per 10 s.
- Plain build (Wrapper-Wayland-PIPETTO-TEST-24acc19), zero-copy off, present wait on: `wrapper-wsi: explicit DRM format
  modifiers on`, swapchains XR24 modifier 0x0500000000000001, compositor copy path, ~110 fps in the menu.
- The black screen / no sound / steady 19 fps seen first was the game pausing itself without window focus (DiRT mutes
  and throttles when unfocused); one tap on the screen fixes it. Not a wrapper issue; focus handoff on Wayland is a
  separate app/compositor follow-up.
- leegao build (da89209) vkCreateDevice fault: not root-caused, superseded by the Pipetto lineage.

## 2026-09-28 21:30 -- tear-safe + fast zero-copy (pipetto-ahb)

- Cause (read from the Pipetto tree ecdd0da): the wrapper's vk_physical_device has no vk_sync types, so
  src/vulkan/wsi/wsi_common.c wsi_signal_semaphore_for_image() / wsi_signal_fence_for_image() (~l.1234 / ~l.1278)
  take their `supported_sync_types == NULL` branch and import SYNC_FD fd -1 ("already signalled") into the
  program's semaphore / fence. The dma-buf's fences (our render fence + the display's release fence the compositor
  imports before wl_buffer.release) are never waited for: the old AHB build's 3632 fps D3D11 came from rendering
  into buffers the display may still scan out. Setting supported_sync_types is not an option: the semaphore / fence
  handles are the real driver's (wrapper trampolines), not vk_semaphore / vk_fence objects.
- Fix (banner/ahb/banner_ahb_wsi_mesa242.py):
  1. wsi_common.c: for a chain with wsi_swapchain.banner_wait_dma_buf (set only by the Wayland WSI, for NATIVE
     dma-buf chains with implicit sync; BANNER_WSI_NO_DMABUF_WAIT=1 = off) the NULL-sync branch exports the dma-buf's
     fences (DMA_BUF_IOCTL_EXPORT_SYNC_FILE, RW) and imports that sync_file through the driver's
     vkImportSemaphoreFdKHR / vkImportFenceFdKHR (SYNC_FD, temporary); refusal / no ioctl -> fd -1 as before, logged
     once. X11 chains never set the flag -> unchanged.
  2. Acquire order from Banners-Turnip (idle > own-render-only > display-held, poll timeout 0).
  3. +2 images for gralloc MAILBOX / IMMEDIATE chains (BANNER_WSI_AHB_EXTRA_IMAGES=0..4).
  Dry-applied to both trees (Pipetto ecdd0da and this leegao tree). Build check: the AHB .so must carry the new
  log string + the env name. pipetto-ahb package renamed Wrapper-Wayland-PIPETTO-AHB-TSAFE-TEST-<sha>.
- Run 16 `36508286968` (333fa5d, headSha verified): started.
- Run 16 `36508286968` (333fa5d): SUCCESS on all four legs; pipetto-ahb log: "tear-safe acquire (dma-buf fences) + acquire order + extra images: in". STAGED /sdcard/Download/Wayland/Wrapper-Wayland-PIPETTO-AHB-TSAFE-TEST-333fa5d.zip sha256 8f4a6a7ba8689dfc32e1efb564756e21acc9b6b783ba31bcb5e80c2cc692c04b (.so f752e895…); installed as imported:Wrapper-Wayland-PIPETTO-AHB-TSAFE-333fa5d.

## 2026-09-28 21:45 -- DEVICE-PROVEN: tear-safe zero-copy wrapper (333fa5d), Pocket FIT, AIO --sweep 15, uncapped mailbox

| AIO avg fps | Vk | GL | D12 | D11 | D10 | D9 | D8 | DDraw |
|---|---|---|---|---|---|---|---|---|
| TSAFE 333fa5d, zc on  | 522 | 265 | 375 | 3438 | 358 | 242 | 241 | 238 |
| TSAFE 333fa5d, zc off | 499 | 240 | 386 | 3307 | 357 | 241 | 241 | 228 |
| old AHB 24acc19, zc on (no fence wait) | 527 | 266 | 371 | 3679 | 358 | - | - | - |
| native Turnip ZCFIX f7ac07e, zc on | 541 | 269 | 378 | 3658 | 355 | 241 | 242 | 239 |

- wine_debug.log: "wrapper-wsi: acquire waits on the dma-buf's fences (sync_file into the program's semaphore /
  fence, SYNC_FD temporary import)" (no refusal / no-ioctl lines), "banner-ahb: 1280x720 swapchain (7 images) on
  gralloc buffers: UBWC", and once "banner-ahb: every free image is still held by the display: the acquire waits
  for it (7 images)" -- the display-fenced class is seen and waited for.
- Compositor: "AHB swapchain (7 images ... UBWC)", "presenting window 0x10084 ... without a copy", ~1200 zero-copy
  frames / 10 s. Screenshots (slots 1/3/4) clean.
- zc off: copy path ("dma-buf: copied into the screen swapchain"), 5-image chains (no bump), fence wait active,
  numbers level with the old plain build (p1: Vk 562 / D11 3411, within run noise).
- Nit: the +2 images also go to a chain created while zero-copy is on whose format has no gralloc equivalent
  (BGRA vkformat 44 -> 6 images, standard buffers). Harmless; the same holds for Banners-Turnip's patch.

## 2026-09-29 08:10 -- D3D12 (vkd3d-proton) capped at ~614 fps on Wayland: root cause = KGSL zero-timeout "poll" blocks

- Symptom (device, Pocket FIT, container 3, uncapped): D3D12 demo X11 809 fps / GPU 86 % vs Wayland 614 / 77 %;
  D3D12HelloTriangle Wayland 597. None of latency frames / present wait / zero-copy / extra images / maxFrameLatency moved it.
- Profile: simpleperf --trace-offcpu -e cpu-clock --call-graph fp (fp unwinds through the stripped Turnip, dwarf did not);
  /sdcard/Download/perf-harness/demo-results/{wl3,x3}*. Wayland: the vkd3d_queue thread spends 91 % of its time inside
  the driver's vkQueueSubmit2 (wrapper_QueueSubmit2 -> libvulkan_freedreno +a2bd32 -> +a2bf08), 63 % on mtx_lock and
  25 % in kgsl_ioctl_device_waittimestamp_ctxtid -> adreno_drawctxt_wait (sleeping). X11 has the same shape, milder
  (74 % / 41 % / 31 %). The swapchain thread is 79 % idle (futex); it is not the bottleneck.
- Disassembly of Turnip v26.3.0-20260929-r2: +a2bf08 = vk_sync_timeline_alloc_point(): mtx_lock(state->mutex) then
  bl +a2c93c(device, state, drain=false) = vk_sync_timeline_gc_locked(), which polls every pending point with
  vk_sync_wait(abs_timeout 0). Turnip KGSL: kgsl_syncobj_wait() -> wait_timestamp_safe() -> get_relative_ms(0) = 0 ->
  IOCTL_KGSL_DEVICE_WAITTIMESTAMP_CTXTID (0x400c0907, +9ea920) with timeout 0, and KGSL treats timeout 0 as "wait
  forever" (adreno_drawctxt_wait; kgsl_add_event + schedule_timeout in the stacks). So the "poll" sleeps until the oldest
  pending timeline point retires, with the timeline mutex held; vkd3d signals timelines on every submit and its fence
  thread takes the same mutex: CPU and GPU never overlap. Not a WSI / present issue; it is in the driver underneath and
  hits X11 too (less, there the GPU completes sooner), which is why the gap is D3D12-specific (DXVK hides it).
- Fix in the adapter (Turnip itself untouched; the X11 imagefs wrapper untouched): banner/kgsl/banner_kgsl_poll.h,
  applied to the Pipetto tree by build_wayland_wrapper.sh apply_kgsl_poll_fix(). When the physical device reports
  VK_DRIVER_ID_MESA_TURNIP, the wrapper finds the loaded driver (dl_iterate_phdr, ADRENOTOOLS_DRIVER_NAME, itself
  excluded) and points its ioctl GOT slot(s) at a shim that answers only WAITTIMESTAMP_CTXTID with timeout 0 via
  IOCTL_KGSL_CMDSTREAM_READTIMESTAMP_CTXTID (RETIRED) + wrap-safe compare (0 / -1 ETIMEDOUT = VK_TIMEOUT, what the
  caller asked for). Everything else goes to the previous target (libfakeinput's ioctl). BANNER_KGSL_POLL_FIX=0 = off.
  Logs "wrapper-kgsl: zero-timeout poll fix on (...)" and the first converted wait. Build check: strings + dl_iterate_phdr import.
- Run 17 `36563471441` (1872e25, headSha verified): started.
- Run 17 `36563471441` (1872e25): pipetto legs FAILED on the new build check only (dl_iterate_phdr is imported as dl_iterate_phdr@LIBC; the regex wanted an unversioned name). plain/ahb green. Regex fixed.
- Run 18 `36564137071` (3af78e4, headSha verified): SUCCESS on all four legs; pipetto-ahb: "KGSL zero-timeout poll fix: in" + tear-safe checks in. pipetto-ahb .so c3533274…, zip e674de01….

## 2026-09-29 08:00 -- DEVICE: D3D12 demo on the KGSL-poll-fix adapter (3af78e4), Pocket FIT, container 3, uncapped

- Installed as imported:Wrapper-Wayland-D12FIX-3af78e4 (.so c3533274…). wine_debug.log: "wrapper-kgsl: zero-timeout poll
  fix on (.../adrenotools/Mesa Turnip v26.3.0-20260929-r2/libvulkan_freedreno.so: 1 ioctl slot)", first converted wait
  logged; tear-safe acquire line present.
- D3D12 demo (D3D12_x64.exe, 800x600 window, copy path): Wayland 614 fps (bundled f752e895) -> 4335 fps (title; compositor
  34.5k-40.4k GPU frames / 10 s), GPU 86 %, picture correct and animating. X11 reference 809.
- Controls (same session): D3D12 demo on the new build with BANNER_KGSL_POLL_FIX=0: 466 fps (title; compositor 558-596
  frames/s) = back to the old cap -> the fix is the cause. D3D12 demo X11 (imagefs wrapper, untouched): 804-810.
- D3D12HelloTriangle: Wayland bundled adapter 577 -> fixed 1040 (compositor ~1178 frames/s); X11 264.
- AIO --sweep 15, Wayland zero-copy, before (bundled f752e895) -> after (3af78e4):
  Vk 538 -> 533 | GL 267 -> 262 | D12 386 -> 386 | D11 3323 -> 3402 | D10 331 -> 325 | D9 268 -> 268 | D8 271 -> 271 | DDraw 236 -> 238
  (all within run noise; no regression). After-run: "banner-ahb: 1280x720 swapchain (7 images) on gralloc buffers: UBWC",
  tear-safe acquire line, display-held class seen once and waited for, compositor 1398-1440 zero-copy frames / 10 s.
- The AIO D3D12 (386 vs X11 ~410) and D3D10 (325-331 vs X11 ~381) gaps are NOT this cause: the fix leaves them unchanged
  (those scenes keep the GPU busy, so the CPU-side serialization never showed). Separate investigation.
- STAGED /sdcard/Download/Wayland/Wrapper-Wayland-D12FIX-TEST-3af78e4.zip sha256 e674de0192793587e901d68bfc5081e3cfa95b3db496d07413623846e6ae403d
  (= run 18 pipetto-ahb zip; .so c3533274b24092298fff484869fa853309584f4053ca8c1249afee7ff1cc9cdd; meta name still
  Wrapper-Wayland-PIPETTO-AHB-TSAFE-TEST-3af78e4). App swap (not done): app/src/main/assets/wayland/adapter/libvulkan_wrapper.so
  on feat/linux-gamescope-runtime <- run 18 pipetto-ahb libvulkan_wrapper.so.
- Proper upstream fix belongs in Turnip (tu_knl_kgsl.cc): wait_timestamp_safe() / kgsl_syncobj_wait() must not send
  timeout 0 to KGSL; read the retired timestamp instead. It hits the X11 path too (imagefs wrapper, same Turnip).

## 2026-10-03 -- adapter v2 item 1: pass-through dispatch (branch banner/wayland-wsi-passthrough, EDIT ONLY, not built)

- Where the wrapper really is: this repository's src/vulkan/wrapper/ is leegao's tree (the `plain` / `ahb`
  legs, not shipped). The device-proven adapter is the Pipetto wrapper-25 tree, fetched by
  banner/build_wayland_wrapper.sh at PIPETTO_REF and patched with anchored edits. Pass-through follows that
  pattern: banner/passthrough/ (banner_passthrough.[ch] + banner_passthrough_patch.py), applied by
  `apply_passthrough` on both Pipetto legs after the KGSL poll fix. leegao legs untouched.
- How the Pipetto wrapper dispatches today: every dispatchable handle is wrapped (wrapper_instance /
  wrapper_physical_device / wrapper_device / wrapper_queue / wrapper_command_buffer, each a Mesa vk_object
  with `dispatch_handle` = the handle the Android loader returned); vkGetDeviceProcAddr = Mesa
  vk_device_get_proc_addr over a table built from wrapper_device_entrypoints (the wrapper's own code),
  wsi_device_entrypoints (Mesa WSI) and wrapper_device_trampolines (generated: unwrap arg 0, call the
  driver's table). Non-dispatchable handles are already the driver's; buffer/image/fence side tables are
  keyed by them. So every vkCmd* paid: trampoline -> load wcb->device -> load dispatch_table.X -> call.
- Design (hybrid; full pass-through of VkDevice/VkQueue is not possible): the Android loader underneath
  stores its DeviceData in word[1] of the driver's VkDevice and the Khronos loader above us writes ITS
  dispatch pointer into word[1] of whatever VkDevice we return (vkDestroyDevice / vkGetDeviceQueue /
  vkAllocateCommandBuffers on the Android side read it), and the Mesa runtime + WSI need vk_device /
  vk_queue objects (wsi_QueuePresentKHR, wsi->QueueSubmit, GetDeviceQueue2). VkDevice and VkQueue stay
  wrapped (one-hop trampoline). VkCommandBuffer passes through: the application gets the driver's command
  buffers and vkGetDeviceProcAddr returns the driver's function (device->dispatch_table.X, loaded through
  the Android loader's GDPA = Turnip's own tu_Cmd*) for every VkCommandBuffer entry point -- 213 of the
  508 device entry points in vk.xml (the other 295 take a VkDevice/VkQueue), i.e. all vkCmd* +
  Begin/End/ResetCommandBuffer. Nothing on the
  Android side reads a command buffer's loader word after allocation (no vkCmd* is hooked there), so the
  Khronos loader overwriting it is harmless; Turnip only uses the word as the loader's.
- Per device: pass = BANNER_WRAPPER_PASSTHROUGH != 0 && physical->emulate_bcn == 0 (Turnip and
  BC-capable Qualcomm blobs: 0; Mali / Samsung / PowerVR / old blobs emulate BCn by intercepting
  vkCmdCopyBufferToImage on wrapped command buffers -> those devices stay fully wrapped, logged as such).
  One stderr line at vkCreateDevice: `wrapper-dispatch: pass-through (N entry points intercepted; M
  VkCommandBuffer entry points are the driver's own, K VkDevice/VkQueue entry points keep the one-hop
  trampoline)` or `wrapper-dispatch: full wrapping (<reason>; N intercepted, K trampolined)`. N/M/K are
  counted slot by slot from the live tables (wrapper table vs plain trampolines vs driver table).
- Still intercepted on a pass-through device (the wrapper runs code, not just a hop): CreateDevice,
  DestroyDevice, GetDeviceQueue(2), GetDeviceProcAddr, CreateBuffer, BindBufferMemory, DestroyBuffer,
  CreateImage, CreateImageView, DestroyImage, CreateFence, WaitForFences, DestroyFence, CreateShaderModule
  (Mali SPIR-V patches), AllocateCommandBuffers (driver call + side-table insert), FreeCommandBuffers,
  DestroyCommandPool, QueueSubmit, QueueSubmit2 (pass-through branch: submits forwarded untouched),
  SetPrivateData, GetPrivateData, AllocateMemory / FreeMemory / MapMemory2KHR / UnmapMemory /
  UnmapMemory2KHR (placed mapping), plus the Mesa WSI device entry points (swapchain, acquire, present,
  present wait, swapchain maintenance). CmdExecuteCommands and CmdCopyBufferToImage are no longer
  reached (driver's own). Physical-device / instance shaping (extension filtering, features, properties
  rename "Wrapper(%s)", image-format BCn answers, graphics_env_hooks, every WRAPPER_* knob) unchanged.
- Side table: driver VkCommandBuffer -> {device, pool} (util hash_table_u64 + simple_mtx), filled by
  vkAllocateCommandBuffers, emptied by vkFreeCommandBuffers / vkDestroyCommandPool / vkDestroyDevice.
  Users: generated `banner_pt_command_buffer_trampolines` (one per VkCommandBuffer entry point, added to
  vk_wrapper_trampolines_gen.py), returned by vkGetInstanceProcAddr and by the WSI's proc_addr
  (wrapper_wsi_proc_addr -> wsi_device_init) for VkCommandBuffer names while pass-through is enabled,
  because the Mesa runtime's generic vk_device_trampolines would read a driver handle as a
  vk_command_buffer. They fall back to the wrapped path for a fully wrapped device. Hot path untouched:
  vkGetDeviceProcAddr hands the driver pointer, no lookup per call.
- Switch: BANNER_WRAPPER_PASSTHROUGH=0 -> every device full wrapping, instance GPA / WSI proc_addr
  unchanged (the old dispatch exactly). Default on.
- X11: this .so's X11 WSI shares the change (dispatch only, platform-neutral); the imagefs X11 wrapper is
  a different binary and untouched. The X11 WSI's AHB blit path records command buffers through the
  WSI's proc_addr -> side-table trampolines, covered.
- CI build check (package_check, pipetto legs): "wrapper-dispatch: pass-through", "wrapper-dispatch: full
  wrapping" and "BANNER_WRAPPER_PASSTHROUGH" strings must be in the .so.
- Verified locally: the patch script applies to the pinned Pipetto wrapper sources (anchors all hit); the
  patched trampolines generator renders with mako against this tree's vk.xml (213 banner_pt_tramp_*,
  557 wrapper_tramp_* = 508 device + 49 physical-device, includes resolve). NOT compiled, NOT device-tested.
- What to measure (A/B, same container, same Turnip, Wayland zero-copy on; X11 untouched):
  1. wine_debug.log shows `wrapper-dispatch: pass-through (...)` once per device; with
     BANNER_WRAPPER_PASSTHROUGH=0 `wrapper-dispatch: full wrapping (BANNER_WRAPPER_PASSTHROUGH=0; ...)`.
  2. AIO graphics test --sweep 15 (all 8 APIs) pass-through vs =0; expect D3D11/D3D9/D3D8 (draw-call bound,
     DXVK) to move, D3D12 and Vk to move less; run-noise reference is the 2026-09-29 before/after table.
  3. A draw-call-heavy DXVK game (DiRT Showdown race, GoW) fps + HUD CPU frame time, pass-through vs =0.
  4. Correctness: BCn-emulating driver (Qualcomm blob / Mali) must log "full wrapping (BCn emulation ...)"
     and render as before; swapchain recreate (alt-tab / resize) and vkd3d-proton (QueueSubmit2) paths.
