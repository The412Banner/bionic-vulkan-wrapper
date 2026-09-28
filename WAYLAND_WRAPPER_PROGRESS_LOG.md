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
