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
