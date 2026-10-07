#!/bin/bash
# Bannerlator: build libvulkan_wrapper.so (leegao's bionic Vulkan wrapper) with the Wayland WSI, on
# GitHub Actions, without Termux's build system.
#
# Why: on Bannerlator's X11 path games run on this wrapper, which loads any community Android Turnip
# (or the Qualcomm blob) underneath through AdrenoTools. The wrapper leegao ships is built with
# -Dplatforms=x11 only, so on the Wayland backend a game cannot use it. This builds the same wrapper
# with -Dplatforms=x11,wayland: one ICD for both backends, one driver pick.
#
# Recipe = leegao/vulkan_wrapper_termux-packages packages/vulkan-wrapper-android/build.sh (same
# configure options, CPPFLAGS, link libraries, SPIR-V Tools static libs), done with the approach of
# The412Banner/Banners-Turnip build_turnip_combined_so.sh: NDK r29 plus a bionic sysroot unpacked from
# the Termux package index. Every fact the result depends on is checked; any miss fails the build.
#
# Differences from leegao's Termux build, all deliberate:
#   * platforms x11,wayland (was x11).
#   * HAVE_WL_DISPATCH_QUEUE_TIMEOUT / HAVE_WL_CREATE_QUEUE_WITH_NAME left off (meson.build), so the
#     .so loads against any libwayland-client >= 1.18 -- a host app may put an older one first.
#   * libc++ linked statically (-static-libstdc++, its archives --exclude-libs'd) instead of NEEDED
#     libc++_shared.so: the imagefs libc++_shared.so is older than NDK r29's headers.
#   * libandroid-shmem is linked from the Termux sysroot, then the NEEDED entry is renamed to
#     libandroid-sysvshm.so, which is what the shipped wrapper uses (Winlator's SysV shm, same
#     libandroid_shm* symbols, present in imagefs/usr/lib).
#   * libadrenotools.so is built from leegao/libadrenotools (the fork the wrapper is written against)
#     with the NDK's CMake, only to link against; at run time the imagefs copy is used, as today.
#   * RUNPATH $ORIGIN (was the Termux prefix).
#   * zlib from the NDK (system libz.so), zstd off -- the shipped wrapper needs neither Termux lib.
#   * DETECT_OS_ANDROID off under -D__TERMUX__ (src/util/detect_os.h), the half of Termux mesa's
#     0000-disable-android-detection.patch that matters: no libcutils/liblog imports, like the
#     shipped wrapper. (vk_android_native_buffer.h keeps its Android branch: the X11 WSI's AHB
#     path needs native_handle_t from it; header-only.)
#
# Environment (all optional): SPIRV_TOOLS_REF, SPIRV_HEADERS_REF, ADRENOTOOLS_REF, OUT_DIR, WITH_AHB=1
# (apply the banner_ahb_v1 zero-copy patch, banner/ahb/).

set -eo pipefail

red='\033[0;31m'; green='\033[0;32m'; nc='\033[0m'
die(){ echo -e "${red}[wrapper-wayland] $*${nc}" >&2; exit 1; }
log(){ echo -e "${green}[wrapper-wayland]${nc} $*"; }

repo="$(cd "$(dirname "$0")/.." && pwd)"
work="${WORK_DIR:-$repo/_work}"
out="${OUT_DIR:-$repo/_out}"
ndkver="android-ndk-r29"
ndkroot="$work/$ndkver"
ndk="$ndkroot/toolchains/llvm/prebuilt/linux-x86_64/bin"
api=26          # TERMUX_PKG_API_LEVEL of leegao's recipe (AHardwareBuffer_* need 26)
termux_repo="https://packages-cf.termux.dev/apt/termux-main"
termux_pkgs="libwayland libwayland-protocols libdrm libffi libandroid-support libandroid-shmem
 libx11 libxcb libxau libxdmcp xorgproto libxrandr libxrender libxext libxfixes libxshmfence"
sysroot="$work/termux"
tprefix="$sysroot/data/data/com.termux/files/usr"
build="$work/build"

# SOURCE=leegao (default): this repository (leegao/bionic-vulkan-wrapper, Mesa 24.2).
# SOURCE=pipetto: Pipetto-crypto/mesa wrapper-25 (Mesa 25.0) -- the lineage of the libvulkan_wrapper.so
# Bannerlator actually ships in imagefs (its strings: "Wrapper(%s)", spirv_patcher.cpp,
# wsi_common_android.c, WRAPPER_SAFE_CREATE_DEVICE / WRAPPER_DMAHEAP_CACHED). libadrenotools comes
# from its own meson subproject there.
SOURCE="${SOURCE:-leegao}"
PIPETTO_REF="${PIPETTO_REF:-ecdd0da8c47b67892b0077130a0152ba924919cb}"   # wrapper-25, 2026-09-17
mesa="$repo"; [ "$SOURCE" = pipetto ] && mesa="$work/pipetto"
SPIRV_TOOLS_REF="${SPIRV_TOOLS_REF:-9113deed32ba366b765a148f474ca86c3890db6a}"      # leegao/SPIRV-Tools main
SPIRV_HEADERS_REF="${SPIRV_HEADERS_REF:-97e96f9e9defeb4bba3cfbd034dec516671dd7a3}"  # its DEPS pin
ADRENOTOOLS_REF="${ADRENOTOOLS_REF:-master}"                                        # leegao/libadrenotools

fetch(){ curl -fsSL --retry 5 --retry-delay 10 --retry-all-errors "$1" -o "$2" || die "download failed: $1"; }

clone_at(){	# <url> <dir> <ref>
	rm -rf "$2"; git init -q "$2"; git -C "$2" remote add origin "$1"
	local i
	for i in 1 2 3 4; do git -C "$2" fetch -q --depth=1 origin "$3" && break; sleep 20; done
	git -C "$2" checkout -q FETCH_HEAD || die "cannot fetch $1 @ $3"
	log "$(basename "$2") = $(git -C "$2" rev-parse HEAD)"
}

prepare(){
	mkdir -p "$work" "$out"; cd "$work"
	if [ ! -x "$ndk/aarch64-linux-android$api-clang" ]; then
		log "downloading $ndkver"
		fetch "https://dl.google.com/android/repository/$ndkver-linux.zip" ndk.zip
		unzip -q ndk.zip && rm ndk.zip
	fi
	[ -x "$ndk/aarch64-linux-android$api-clang" ] || die "NDK clang for API $api missing"

	log "Termux sysroot: $(echo $termux_pkgs)"
	fetch "$termux_repo/dists/stable/main/binary-aarch64/Packages" Packages
	rm -rf "$sysroot" debs && mkdir -p "$sysroot" debs
	for p in $termux_pkgs; do
		fn=$(awk -v P="$p" 'BEGIN{RS="";FS="\n"} {n="";f=""; for(i=1;i<=NF;i++){if($i~/^Package: /)n=substr($i,10); if($i~/^Filename: /)f=substr($i,11)} if(n==P){print f; exit}}' Packages)
		[ -n "$fn" ] || die "Termux package $p not in the index"
		echo " - $fn"
		fetch "$termux_repo/$fn" "debs/$p.deb"
		(cd debs && rm -rf x && mkdir x && cd x && ar x "../$p.deb" && tar -xf data.tar.* -C "$sysroot") || die "cannot unpack $p.deb"
	done
	for l in libwayland-client.so libdrm.so libxcb.so libX11-xcb.so libandroid-shmem.so; do
		[ -e "$tprefix/lib/$l" ] || die "Termux sysroot has no $l"
	done
	[ -f "$tprefix/include/sys/shm.h" ] || die "Termux sysroot has no sys/shm.h (libandroid-shmem)"
	grep -q libandroid_shmget "$tprefix/include/sys/shm.h" || die "Termux sys/shm.h does not map shmget -> libandroid_shmget"
}

build_spirv_tools(){
	log "SPIRV-Tools (leegao fork) static libs, ndk-build"
	clone_at https://github.com/leegao/SPIRV-Tools.git "$work/SPIRV-Tools" "$SPIRV_TOOLS_REF"
	rm -rf "$work/SPIRV-Tools/external/spirv-headers"
	clone_at https://github.com/KhronosGroup/SPIRV-Headers.git "$work/SPIRV-Tools/external/spirv-headers" "$SPIRV_HEADERS_REF"
	cd "$work/SPIRV-Tools"
	mkdir -p build/libs build/app
	"$ndkroot/ndk-build" -j"$(nproc)" -C android_test NDK_PROJECT_PATH=. \
		NDK_LIBS_OUT="$(pwd)/build/libs" NDK_APP_OUT="$(pwd)/build/app" APP_PLATFORM=android-$api HOST_PYTHON=python3 \
		SPIRV-Tools SPIRV-Tools-opt || die "ndk-build SPIRV-Tools failed"
	local a=build/app/local/arm64-v8a
	[ -f $a/libSPIRV-Tools.a ] && [ -f $a/libSPIRV-Tools-opt.a ] || die "SPIRV-Tools static libs missing: $(ls $a 2>&1)"
	mkdir -p "$repo/src/vulkan/wrapper/lib"
	rm -f "$repo/src/vulkan/wrapper/lib/"*.a
	cp $a/libSPIRV-Tools.a $a/libSPIRV-Tools-opt.a "$repo/src/vulkan/wrapper/lib/"
	# Headers must match the libs (pull_spirv_tools.sh does the same). Show what changed vs the
	# committed copy so a drift is visible in the log.
	diff -r "$repo/src/vulkan/wrapper/include/spirv-tools/spirv-tools" include/spirv-tools > "$out/spirv-headers-diff.txt" \
		&& log "committed spirv-tools headers match the fork" \
		|| log "spirv-tools headers differ from the committed copy (using the fork's; see spirv-headers-diff.txt)"
	rm -rf "$repo/src/vulkan/wrapper/include/spirv-tools"
	cp -r include "$repo/src/vulkan/wrapper/include/spirv-tools"
	ls -la "$repo/src/vulkan/wrapper/lib/"
}

build_adrenotools(){
	log "libadrenotools (leegao fork), NDK CMake -- link-time only"
	rm -rf "$work/libadrenotools"
	git clone -q --recursive https://github.com/leegao/libadrenotools.git "$work/libadrenotools" || die "clone libadrenotools failed"
	git -C "$work/libadrenotools" checkout -q "$ADRENOTOOLS_REF"
	git -C "$work/libadrenotools" submodule update -q --init --recursive
	log "libadrenotools = $(git -C "$work/libadrenotools" rev-parse HEAD)"
	cmake -S "$work/libadrenotools" -B "$work/libadrenotools/build" -G Ninja \
		-DCMAKE_TOOLCHAIN_FILE="$ndkroot/build/cmake/android.toolchain.cmake" \
		-DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-$api -DBUILD_SHARED_LIBS=ON \
		-DCMAKE_SHARED_LINKER_FLAGS=-llog \
		-DCMAKE_BUILD_TYPE=Release || die "cmake configure libadrenotools failed"
	cmake --build "$work/libadrenotools/build" --target adrenotools || die "libadrenotools build failed"
	mkdir -p "$work/adrenotools-lib"
	cp -L "$work/libadrenotools/build/libadrenotools.so" "$work/adrenotools-lib/" || die "libadrenotools.so not produced"
	local ats; ats="$("$ndk/llvm-readelf" --dyn-syms -W "$work/adrenotools-lib/libadrenotools.so")"
	grep -qE ' adrenotools_open_libvulkan$' <<< "$ats" \
		|| { echo "$ats" | head -60; die "built libadrenotools.so does not export adrenotools_open_libvulkan"; }
	log "libadrenotools SONAME: $("$ndk/llvm-readelf" -d "$work/adrenotools-lib/libadrenotools.so" | grep SONAME || echo none)"
}

fetch_pipetto(){
	log "Pipetto-crypto/mesa @ $PIPETTO_REF"
	clone_at https://github.com/Pipetto-crypto/mesa.git "$mesa" "$PIPETTO_REF"
	[ "$(git -C "$mesa" rev-parse HEAD)" = "$PIPETTO_REF" ] || die "Pipetto checkout is not $PIPETTO_REF"
	# The same two source changes this repository carries in-tree (see its meson.build and
	# src/util/detect_os.h), applied to the Pipetto tree with asserted anchors.
	python3 - "$mesa" <<'PY2' || die "Pipetto source patches failed"
import sys, os
root = sys.argv[1]
def patch(rel, old, new):
    p = os.path.join(root, rel); s = open(p).read()
    assert s.count(old) == 1, (rel, old[:60]); open(p, 'w').write(s.replace(old, new))
for d in ("wl_display_dispatch_queue_timeout", "wl_display_create_queue_with_name"):
    define = "HAVE_WL_DISPATCH_QUEUE_TIMEOUT" if "timeout" in d else "HAVE_WL_CREATE_QUEUE_WITH_NAME"
    patch("meson.build",
          "  if cc.has_function(\n      '%s',\n      prefix : '#include <wayland-client.h>',\n"
          "      dependencies: dep_wayland_client)\n    pre_args += ['-D%s']\n  endif\n" % (d, define),
          "  message('wayland: %s left off (old-libwayland compat)')\n" % define)
patch("src/util/detect_os.h", "#if defined(__ANDROID__)\n#define DETECT_OS_ANDROID 1\n#endif\n",
      "/* Bannerlator: not the Android platform on a -D__TERMUX__ build (Termux mesa 0000-disable-android-detection). */\n"
      "#if defined(__ANDROID__) && !defined(__TERMUX__)\n#define DETECT_OS_ANDROID 1\n#endif\n")
print("pipetto: old-libwayland compat + Android detection off applied")

# Explicit DRM format modifiers for the Wayland WSI. The wrapper never set
# wsi_device.supports_modifiers (drivers do this themselves after wsi_device_init), so
# wsi_common_wayland.c took the legacy path: no modifier list, image created with the "scanout"
# flag, dma-buf shared with DRM_FORMAT_MOD_INVALID -- which Bannerlator's compositor cannot import
# (device test 2026-09-28 18:30: white screen, "could not import GPU frames ... modifier
# 0xffffffffffffff"). Turn it on when the driver underneath has both extensions the modifier path
# needs; BANNER_WSI_NO_MODIFIERS=1 turns it back off.
patch("src/vulkan/wrapper/wrapper_physical_device.c",
      "      pdevice->vk.wsi_device = &pdevice->wsi_device;\n",
      "      pdevice->vk.wsi_device = &pdevice->wsi_device;\n"
      "      {\n"
      "         const char *nomod = getenv(\"BANNER_WSI_NO_MODIFIERS\");\n"
      "         pdevice->wsi_device.supports_modifiers =\n"
      "            pdevice->base_supported_extensions.EXT_image_drm_format_modifier &&\n"
      "            pdevice->base_supported_extensions.EXT_external_memory_dma_buf &&\n"
      "            !(nomod && nomod[0] == '1');\n"
      "         fprintf(stderr, \"wrapper-wsi: explicit DRM format modifiers %s (driver: drm_format_modifier=%d dma_buf=%d)\\n\",\n"
      "                 pdevice->wsi_device.supports_modifiers ? \"on\" : \"off\",\n"
      "                 pdevice->base_supported_extensions.EXT_image_drm_format_modifier,\n"
      "                 pdevice->base_supported_extensions.EXT_external_memory_dma_buf);\n"
      "      }\n")
patch("src/vulkan/wrapper/wrapper_physical_device.c", "#include <math.h>\n",
      "#include <math.h>\n#include <stdio.h>\n#include <stdlib.h>\n")
# The X11 path of this tree (__TERMUX__) presents AHardwareBuffers (WSI_IMAGE_TYPE_ANDROID) and
# never used modifiers; keep its DRI3 modifier re-query off now that supports_modifiers can be true.
patch("src/vulkan/wsi/wsi_common_x11.c",
      "wsi_x11_swapchain_query_dri3_modifiers_changed(struct x11_swapchain *chain)\n{\n",
      "wsi_x11_swapchain_query_dri3_modifiers_changed(struct x11_swapchain *chain)\n{\n"
      "#ifdef __TERMUX__\n   return false; /* Bannerlator: AHB presentation, no DRI3 modifiers */\n#endif\n")
# One line per Wayland swapchain: what its buffers were shared as.
patch("src/vulkan/wsi/wsi_common_wayland.c",
      "   chain->present_ids.valid_refresh_nsec = false;\n",
      "   fprintf(stderr, \"wrapper-wsi: wayland swapchain %ux%u vkformat %d drm 0x%08x: %u images, %s, modifier 0x%016llx\\n\",\n"
      "           chain->extent.width, chain->extent.height, chain->vk_format, chain->drm_format,\n"
      "           chain->base.image_count,\n"
      "           chain->buffer_type == WSI_WL_BUFFER_NATIVE ? \"dma-buf\" : \"shm\",\n"
      "           chain->base.image_count ? (unsigned long long)chain->images[0].base.drm_modifier : 0ull);\n"
      "   chain->present_ids.valid_refresh_nsec = false;\n")
print("pipetto: explicit modifiers + per-swapchain log applied")
PY2
	apply_kgsl_poll_fix
	apply_mali_switches
}

# Zero-timeout KGSL timestamp waits as real polls (banner/kgsl/banner_kgsl_poll.h has the why): the
# header goes next to the wrapper sources, and the wrapper points the Turnip driver's ioctl GOT slot at
# it once the physical device says the driver is Turnip.
apply_kgsl_poll_fix(){
	cp "$repo/banner/kgsl/banner_kgsl_poll.h" "$mesa/src/vulkan/wrapper/banner_kgsl_poll.h"
	python3 - "$mesa" <<'PY3' || die "KGSL poll fix patch failed"
import sys, os
root = sys.argv[1]
def patch(rel, old, new):
    p = os.path.join(root, rel); s = open(p).read()
    assert s.count(old) == 1, (rel, old[:60]); open(p, 'w').write(s.replace(old, new))
patch("src/vulkan/wrapper/wrapper_physical_device.c", "#include <math.h>\n",
      "#include <math.h>\n#include \"banner_kgsl_poll.h\"\n")
patch("src/vulkan/wrapper/wrapper_physical_device.c",
      "      pdevice->dispatch_table.GetPhysicalDeviceProperties2(\n"
      "         pdevice->dispatch_handle, &pdevice->properties2);\n",
      "      pdevice->dispatch_table.GetPhysicalDeviceProperties2(\n"
      "         pdevice->dispatch_handle, &pdevice->properties2);\n"
      "      if (pdevice->driver_properties.driverID == VK_DRIVER_ID_MESA_TURNIP)\n"
      "         banner_kgsl_poll_fix(); /* Bannerlator: KGSL zero-timeout waits = polls */\n")
print("pipetto: KGSL zero-timeout poll fix applied")
PY3
}

# Mali (PanVK) compatibility switches, all off by default (banner/mali/banner_mali.h has the why):
# BANNER_MALI_HIDE_EXTS, BANNER_MALI_NO_SUBMIT_WAITS, BANNER_MALI_NO_ACQUIRE_SIGNAL. Ported from
# FristOneRR-Wrapperv1 (MIT, https://github.com/FristOneRR-Admin/FristOneRR-Wrapperv1 commit 834d827e),
# where they are always on. Runs before apply_ahb; its anchors do not overlap the AHB patch's.
apply_mali_switches(){
	cp "$repo/banner/mali/banner_mali.h" "$mesa/src/vulkan/wrapper/banner_mali.h"
	python3 - "$mesa" <<'PY4' || die "Mali switches patch failed"
import sys, os
root = sys.argv[1]
def patch(rel, old, new):
    p = os.path.join(root, rel); s = open(p).read()
    assert s.count(old) == 1, (rel, old[:60]); open(p, 'w').write(s.replace(old, new))
CREDIT = "from FristOneRR-Wrapperv1 (MIT, github.com/FristOneRR-Admin/FristOneRR-Wrapperv1 834d827e)"
# The one translation unit that owns banner_mali_flags and reads the environment (constructor).
patch("src/vulkan/wrapper/wrapper_physical_device.c", "#include \"banner_kgsl_poll.h\"\n",
      "#include \"banner_kgsl_poll.h\"\n#define BANNER_MALI_IMPL\n#include \"banner_mali.h\"\n")
# 1. BANNER_MALI_HIDE_EXTS: no present_id / present_wait / dynamic_rendering extension (1.3 core stays).
patch("src/vulkan/wrapper/wrapper_physical_device.c",
      "      supported_features->swapchainMaintenance1 = true;\n",
      "      /* Bannerlator BANNER_MALI_HIDE_EXTS, " + CREDIT + ":\n"
      "       * hide VK_KHR_present_id, VK_KHR_present_wait and the VK_KHR_dynamic_rendering extension\n"
      "       * (Vulkan 1.3 core dynamic rendering stays) so DXVK 2.x runs on FristOneRR PanVK. */\n"
      "      if (banner_mali_flags & BANNER_MALI_HIDE_EXTS) {\n"
      "         pdevice->vk.supported_extensions.KHR_present_id = false;\n"
      "         pdevice->vk.supported_extensions.KHR_present_wait = false;\n"
      "         pdevice->vk.supported_extensions.KHR_dynamic_rendering = false;\n"
      "         supported_features->presentId = false;\n"
      "         supported_features->presentWait = false;\n"
      "      }\n"
      "      supported_features->swapchainMaintenance1 = true;\n")
# 2. BANNER_MALI_NO_SUBMIT_WAITS: drop wait semaphores in vkQueueSubmit / vkQueueSubmit2. FristOneRR
#    does it for up to 8 submits (its stack copy); the wrapper already copies every submit, so all.
patch("src/vulkan/wrapper/wrapper_device.c", "#include \"wrapper_trampolines.h\"\n",
      "#include \"wrapper_trampolines.h\"\n#include \"banner_mali.h\"\n")
patch("src/vulkan/wrapper/wrapper_device.c",
      "      wrapper_submits[i] = pSubmits[i];\n"
      "      wrapper_submits[i].pCommandBuffers = command_buffers;\n",
      "      wrapper_submits[i] = pSubmits[i];\n"
      "      wrapper_submits[i].pCommandBuffers = command_buffers;\n"
      "      /* Bannerlator BANNER_MALI_NO_SUBMIT_WAITS, " + CREDIT + ":\n"
      "       * no wait semaphores. Only safe on a driver that runs one queue strictly in order. */\n"
      "      if (banner_mali_flags & BANNER_MALI_NO_SUBMIT_WAITS) {\n"
      "         wrapper_submits[i].waitSemaphoreCount = 0;\n"
      "         wrapper_submits[i].pWaitSemaphores = NULL;\n"
      "         wrapper_submits[i].pWaitDstStageMask = NULL;\n"
      "      }\n")
patch("src/vulkan/wrapper/wrapper_device.c",
      "      wrapper_submits[i] = pSubmits[i];\n"
      "      wrapper_submits[i].pCommandBufferInfos = command_buffers;\n",
      "      wrapper_submits[i] = pSubmits[i];\n"
      "      wrapper_submits[i].pCommandBufferInfos = command_buffers;\n"
      "      /* Bannerlator BANNER_MALI_NO_SUBMIT_WAITS, " + CREDIT + ":\n"
      "       * no wait semaphores. Only safe on a driver that runs one queue strictly in order. */\n"
      "      if (banner_mali_flags & BANNER_MALI_NO_SUBMIT_WAITS) {\n"
      "         wrapper_submits[i].waitSemaphoreInfoCount = 0;\n"
      "         wrapper_submits[i].pWaitSemaphoreInfos = NULL;\n"
      "      }\n")
# 3. BANNER_MALI_NO_ACQUIRE_SIGNAL: the acquire leaves the program's semaphore / fence alone (no fd -1
#    import, no dma-buf sync_file import). banner_mali.h only sets it together with NO_SUBMIT_WAITS.
patch("src/vulkan/wsi/wsi_common.c", "#include \"vk_util.h\"\n",
      "#include \"vk_util.h\"\n#include <stdio.h>\n#include \"../wrapper/banner_mali.h\"\n")
patch("src/vulkan/wsi/wsi_common.c",
      "   image->acquired = true;\n\n"
      "   if (pAcquireInfo->semaphore != VK_NULL_HANDLE) {\n",
      "   image->acquired = true;\n\n"
      "   /* Bannerlator BANNER_MALI_NO_ACQUIRE_SIGNAL, " + CREDIT + ":\n"
      "    * do not signal the acquire semaphore / fence. Only with BANNER_MALI_NO_SUBMIT_WAITS (the wait on\n"
      "    * the semaphore is dropped there); also skips the Wayland chains' tear-safe dma-buf wait. */\n"
      "   const bool banner_mali_no_signal = banner_mali_flags & BANNER_MALI_NO_ACQUIRE_SIGNAL;\n"
      "   if (banner_mali_no_signal) {\n"
      "      static bool said;\n"
      "      if (!said) {\n"
      "         said = true;\n"
      "         fprintf(stderr, \"wrapper-mali: acquire semaphore / fence left unsignalled (first acquire)\\n\");\n"
      "      }\n"
      "   }\n\n"
      "   if (pAcquireInfo->semaphore != VK_NULL_HANDLE && !banner_mali_no_signal) {\n")
patch("src/vulkan/wsi/wsi_common.c",
      "   if (pAcquireInfo->fence != VK_NULL_HANDLE) {\n"
      "      VkResult signal_result =\n"
      "         wsi_signal_fence_for_image(",
      "   if (pAcquireInfo->fence != VK_NULL_HANDLE && !banner_mali_no_signal) {\n"
      "      VkResult signal_result =\n"
      "         wsi_signal_fence_for_image(")
print("pipetto: Mali switches (BANNER_MALI_HIDE_EXTS / NO_SUBMIT_WAITS / NO_ACQUIRE_SIGNAL, default off) applied")
PY4
}

apply_ahb(){
	[ "${WITH_AHB:-0}" = 1 ] || { log "banner_ahb_wsi: not requested"; return 0; }
	# Mesa 24.2 port of Banners-Turnip patches/wayland/banner_ahb_wsi.py (same helpers, this tree's
	# anchors); the protocol glue sits next to it.
	log "applying banner/ahb/banner_ahb_wsi_mesa242.py"
	# Its anchors also match the Pipetto tree's Mesa 25.0 WSI (dry-applied 2026-09-28).
	python3 "$repo/banner/ahb/banner_ahb_wsi_mesa242.py" "$mesa" || die "banner_ahb_wsi_mesa242.py did not apply"
}

configure_build(){
	cd "$mesa"
	command -v glslangValidator >/dev/null || die "glslangValidator missing"
	pkg-config --exists wayland-scanner || die "native wayland-scanner.pc missing (apt libwayland-dev)"
	log "native wayland-scanner $(pkg-config --modversion wayland-scanner)"

	local inc="-I$tprefix/include"
	# -fno-emulated-tls: ELF TLS (Android 10+), as the shipped wrapper (no __emutls_get_address import,
	# which nothing on the device exports). NOT __ANDROID_UNAVAILABLE_SYMBOLS_ARE_WEAK__: it makes meson
	# "find" qsort_r / pthread_*affinity_np (API 36) and the .so then calls a null weak symbol on older
	# Android (seen in run 8).
	local defs="-D__TERMUX__ -D__USE_GNU -D__ANDROID__ -fno-emulated-tls"
	local warn="-Wno-error -Wno-deprecated-declarations -Wno-incompatible-pointer-types -Wno-incompatible-pointer-types-discards-qualifiers -Wno-int-conversion"
	local libs="-L$tprefix/lib -L$work/adrenotools-lib -landroid-shmem -ladrenotools"
	local extra_opts="-Dcpp_rtti=false"
	if [ "$SOURCE" = pipetto ]; then
		# libadrenotools = the tree's own meson subproject. Termux's headers used to pull <fcntl.h> in;
		# force it rather than patch each file.
		libs="-L$tprefix/lib -landroid-shmem"
		defs="$defs -include fcntl.h"
		extra_opts=""
	fi
	# C++ runtime. leegao: static (its code only needs TLS-free parts of libc++). pipetto: shared,
	# exactly like the shipped wrapper -- its C++ pulls libc++ parts built with emulated TLS
	# (__emutls_get_address, run 10/11: in no Mesa object or subproject archive), and that symbol is
	# exported only by libc++_shared.so. The imagefs copy provides it; package_check verifies every
	# libc++ symbol the .so imports against banner/imagefs-libc++_shared.exports.txt (the export list
	# of imagefs usr/lib/libc++_shared.so, sha256 5a6b0871..., taken from the device 2026-09-28).
	local cxx_link="-static-libstdc++ -Wl,--exclude-libs,libc++_static.a -Wl,--exclude-libs,libc++abi.a -Wl,--exclude-libs,libunwind.a -Wl,--exclude-libs,libSPIRV-Tools.a -Wl,--exclude-libs,libSPIRV-Tools-opt.a"
	[ "$SOURCE" = pipetto ] && cxx_link=""
	# zlib = the NDK's (SONAME libz.so, a public system library), not Termux's libz.so.1.
	mkdir -p "$work/pc"
	cat > "$work/pc/zlib.pc" <<'PC'
Name: zlib
Description: NDK system zlib
Version: 1.3.0
Libs: -lz
Cflags:
PC
	cat > "$work/cross.txt" <<EOF
[binaries]
ar = '$ndk/llvm-ar'
c = '$ndk/aarch64-linux-android$api-clang'
cpp = '$ndk/aarch64-linux-android$api-clang++'
c_ld = 'lld'
cpp_ld = 'lld'
strip = '$ndk/llvm-strip'
pkg-config = '/usr/bin/pkg-config'

[properties]
sys_root = '$sysroot'
pkg_config_libdir = ['$tprefix/lib/pkgconfig', '$tprefix/share/pkgconfig', '$work/pc']

[built-in options]
c_args = [$(for f in $inc $defs $warn; do printf "'%s', " "$f"; done)]
cpp_args = [$(for f in $inc $defs $warn; do printf "'%s', " "$f"; done)]
c_link_args = [$(for f in $libs; do printf "'%s', " "$f"; done)]
cpp_link_args = [$(for f in $libs $cxx_link; do printf "'%s', " "$f"; done)]

[host_machine]
system = 'android'
cpu_family = 'aarch64'
cpu = 'armv8'
endian = 'little'
EOF
	cat > "$work/native.txt" <<EOF
[binaries]
c = 'clang'
cpp = 'clang++'
ar = 'llvm-ar'
strip = 'llvm-strip'
EOF
	cat "$work/cross.txt"
	rm -rf "$build"
	meson setup "$build" --cross-file "$work/cross.txt" --native-file "$work/native.txt" \
		--prefix /usr --libdir lib --buildtype=release \
		-Db_ndebug=true -Dstrip=true \
		$extra_opts -Dgbm=disabled -Dopengl=false -Dllvm=disabled -Dshared-llvm=disabled \
		-Dplatforms=x11,wayland -Dgallium-drivers= -Dxmlconfig=disabled -Dvulkan-drivers=wrapper \
		-Dzstd=disabled \
		|| { cat "$build/meson-logs/meson-log.txt" | tail -80; die "meson setup failed"; }
	ninja -C "$build" src/vulkan/wrapper/libvulkan_wrapper.so || die "ninja failed"
	# Who references memfd_create (diagnostic only)?
	local a
	shopt -s globstar nullglob
	for a in "$build"/**/*.a "$mesa"/src/vulkan/wrapper/lib/*.a; do
		"$ndk/llvm-nm" -A "$a" 2>/dev/null | grep -E ' U (memfd_create|__emutls_get_address)$' || true
	done
	for a in "$build"/src/vulkan/wrapper/libvulkan_wrapper.so.p/*.o \
	         "$ndkroot"/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/aarch64-linux-android/libc++_static.a \
	         "$ndkroot"/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/aarch64-linux-android/libc++abi.a; do
		"$ndk/llvm-nm" -A "$a" 2>/dev/null | grep -E ' U __emutls_get_address$' | head -5 || true
	done
	shopt -u globstar nullglob
	[ -f "$build/src/vulkan/wrapper/libvulkan_wrapper.so" ] || die "libvulkan_wrapper.so not built"
}

package_check(){
	cd "$repo"
	local re="$ndk/llvm-readelf" so="$out/libvulkan_wrapper.so"
	cp -L "$build/src/vulkan/wrapper/libvulkan_wrapper.so" "$so"
	"$ndk/llvm-strip" --strip-unneeded "$so" || die "llvm-strip failed"
	patchelf --replace-needed libandroid-shmem.so libandroid-sysvshm.so "$so" || fail "patchelf replace-needed failed"
	patchelf --set-rpath '$ORIGIN' "$so" || fail "patchelf set-rpath failed"

	local failures=0
	fail(){ echo -e "${red}[check] $*${nc}" >&2; failures=$((failures+1)); }
	local dyn syms needed
	dyn="$("$re" -d "$so")"; syms="$("$re" --dyn-syms -W "$so")"
	needed="$(echo "$dyn" | grep -oP 'NEEDED.*\[\K[^]]+' | tr '\n' ' ')"
	log "NEEDED: $needed"
	log "SONAME: $(echo "$dyn" | grep -oP 'SONAME.*\[\K[^]]+')"
	log "RUNPATH: $(echo "$dyn" | grep -E 'RUNPATH|RPATH' || echo none)"
	grep -q 'SONAME.*\[libvulkan_wrapper.so\]' <<< "$dyn" || fail "SONAME is not libvulkan_wrapper.so"
	for s in vk_icdGetInstanceProcAddr vk_icdNegotiateLoaderICDInterfaceVersion vk_icdGetPhysicalDeviceProcAddr; do
		grep -qE "FUNC +GLOBAL +DEFAULT +[0-9]+ $s\$" <<< "$syms" || fail "$s not exported"
	done
	# Every NEEDED lib must exist where the guest looks: imagefs/usr/lib (list taken from the device
	# 2026-09-28), the Android system libs, or libwayland-client.so from the Proton wcp's lib/ (which
	# GuestProgramLauncherComponent puts first on LD_LIBRARY_PATH in Wayland mode).
	local ok="libc.so libm.so libdl.so liblog.so libnativewindow.so libandroid.so libsync.so
		libandroid-sysvshm.so libadrenotools.so libdrm.so libxcb.so libX11-xcb.so libX11.so libxcb-dri3.so
		libxcb-present.so libxcb-sync.so libxcb-randr.so libxcb-shm.so libxcb-xfixes.so libxshmfence.so
		libz.so libzstd.so libwayland-client.so libffi.so"
	[ "$SOURCE" = pipetto ] && ok="$ok libc++_shared.so"
	local n
	for n in $needed; do
		grep -q " $n " <<< " $(echo $ok) " || fail "NEEDED $n is not in imagefs/usr/lib, the system, or the Proton lib/"
	done
	grep -q ' libwayland-client.so ' <<< " $needed " || fail "no NEEDED libwayland-client.so: the Wayland WSI is not in"
	grep -q ' libadrenotools.so ' <<< " $needed " || fail "no NEEDED libadrenotools.so"
	if [ "$SOURCE" = pipetto ]; then
		grep -q ' libc++_shared.so ' <<< " $needed " || fail "pipetto: expected NEEDED libc++_shared.so"
		# Every C++-runtime import must exist in the imagefs libc++_shared.so (NDK r29 headers vs an
		# older runtime on the device).
		local cxxmiss
		cxxmiss="$(grep -E ' UND ' <<< "$syms" | awk '{print $NF}' | grep -v '@LIBC' | sed 's/@.*//' \
			| grep -E '^(_Z|__cxa_|__gxx_|_Unwind_|__emutls_|__dynamic_cast)' | LC_ALL=C sort -u \
			| LC_ALL=C comm -23 - "$repo/banner/imagefs-libc++_shared.exports.txt" || true)"
		[ -z "$cxxmiss" ] || fail "C++ runtime symbols the imagefs libc++_shared.so lacks: $(echo $cxxmiss)"
		log "C++ runtime imports: $(grep -E ' UND ' <<< "$syms" | awk '{print $NF}' | grep -cE '^(_Z|__cxa_|__gxx_|_Unwind_|__emutls_)') (all in imagefs libc++_shared.so)"
		strings -a "$so" | grep -q '^Wrapper(%s)$' || fail "no \"Wrapper(%s)\" device-name format (not the shipped lineage?)"
	else
		grep -q ' libc++_shared.so ' <<< " $needed " && fail "libc++_shared.so is NEEDED (expected static libc++)"
	fi
	grep -q ' UND .*adrenotools_open_libvulkan' <<< "$syms" || fail "adrenotools_open_libvulkan not imported"
	local wl; wl="$(echo "$syms" | grep -c ' UND .*wl_' || true)"
	[ "$wl" -gt 10 ] || fail "only $wl wl_* imports: the Wayland WSI is not in"
	grep -q "VK_KHR_wayland_surface" "$so" || fail "VK_KHR_wayland_surface string missing"
	grep -q "VK_KHR_xcb_surface" "$so" || fail "VK_KHR_xcb_surface string missing (X11 WSI dropped?)"
	# Symbols only a newer libwayland-client has (1.23: queue names, dispatch timeout, proxy queue;
	# 1.24: wl_fixes). Any of them imported = will not load against an older libwayland-client.
	local sym
	for sym in wl_display_dispatch_queue_timeout wl_display_create_queue_with_name wl_fixes_interface \
	           wl_proxy_get_queue wl_event_queue_get_name; do
		grep -qE " UND +$sym\$" <<< "$syms" && fail "imports $sym: will not load against an older libwayland-client"
	done
	# Imports nothing on the device provides (BIND_NOW: one unresolved symbol = dlopen fails).
	[ "$SOURCE" != pipetto ] && grep -qE ' UND +__emutls_get_address$' <<< "$syms" && fail "imports __emutls_get_address (emulated TLS)"
	for sym in qsort_r pthread_getaffinity_np pthread_setaffinity_np; do
		grep -qE " UND +$sym\$" <<< "$syms" && fail "imports $sym (API 36; weak-null on older Android)"
	done
	grep -qE ' UND +memfd_create$' <<< "$syms" && log "note: imports memfd_create (libc API 30 = Android 11+)"
	# The fallbacks must not leak out of the .so either.
	grep -vE ' UND ' <<< "$syms" | grep -E ' (wl_display_dispatch_queue_timeout|wl_display_create_queue_with_name)$' >/dev/null \
		&& fail "exports a libwayland symbol (fallback not hidden)"
	echo "$syms" | grep -E ' UND .*wl_' | awk '{print $NF}' | sort > "$out/wl-imports.txt"
	echo "$syms" | grep -vE ' UND ' | grep -E 'FUNC|OBJECT' | grep GLOBAL | awk '{print $NF}' | sort > "$out/exports.txt"
	log "$wl wl_* imports: $(tr '\n' ' ' < "$out/wl-imports.txt")"
	log "exports: $(tr '\n' ' ' < "$out/exports.txt")"
	if [ "$SOURCE" = pipetto ]; then
		grep -q "wrapper-kgsl: zero-timeout poll fix" "$so" || fail "KGSL zero-timeout poll fix missing"
		grep -q "BANNER_KGSL_POLL_FIX" "$so" || fail "BANNER_KGSL_POLL_FIX switch missing"
		grep -qE ' UND +dl_iterate_phdr(@|$)' <<< "$syms" || fail "dl_iterate_phdr not imported (KGSL poll fix not linked in?)"
		log "KGSL zero-timeout poll fix: in"
		for sym in BANNER_MALI_HIDE_EXTS BANNER_MALI_NO_SUBMIT_WAITS BANNER_MALI_NO_ACQUIRE_SIGNAL \
		           "wrapper-mali: hide_exts=" "wrapper-mali: acquire semaphore / fence left unsignalled"; do
			grep -q "$sym" "$so" || fail "Mali switch string missing: $sym"
		done
		log "Mali switches (default off): in"
	fi
	if [ "${WITH_AHB:-0}" = 1 ]; then
		grep -q banner_ahb_v1 "$so" || fail "banner_ahb_v1 missing (zero-copy patch not in)"
		log "banner_ahb_v1: in"
		grep -q "wrapper-wsi: acquire waits on the dma-buf" "$so" || fail "dma-buf acquire wait missing (tear-safe sync not in)"
		grep -q "BANNER_WSI_AHB_EXTRA_IMAGES" "$so" || fail "extra gralloc images missing"
		log "tear-safe acquire (dma-buf fences) + acquire order + extra images: in"
	fi
	echo "$dyn" > "$out/dynamic.txt"
	grep -E ' UND ' <<< "$syms" | awk '{print $NF}' | sort > "$out/undefined.txt"

	# ICD manifest exactly like the shipped one (imagefs/usr/share/vulkan/icd.d/wrapper_icd.aarch64.json).
	cat > "$out/wrapper_icd.aarch64.json" <<'EOF'
{
    "ICD": {
        "api_version": "1.3.289",
        "library_path": "libvulkan_wrapper.so"
    },
    "file_format_version": "1.0.0"
}
EOF
	(cd "$out" && sha256sum libvulkan_wrapper.so > libvulkan_wrapper.so.sha256)
	ls -la "$out"
	[ "$failures" = 0 ] || die "$failures check(s) failed (artifacts kept in $out)"
}

prepare
if [ "$SOURCE" = pipetto ]; then
	fetch_pipetto
	apply_ahb
else
	build_spirv_tools
	build_adrenotools
	apply_ahb
fi
configure_build
package_check
log "done: $out/libvulkan_wrapper.so"
