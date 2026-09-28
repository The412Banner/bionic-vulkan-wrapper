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
