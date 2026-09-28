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
#   * libc++ linked statically (-static-libstdc++, --exclude-libs,ALL) instead of NEEDED
#     libc++_shared.so: the imagefs libc++_shared.so is older than NDK r29's headers.
#   * libandroid-shmem is linked from the Termux sysroot, then the NEEDED entry is renamed to
#     libandroid-sysvshm.so, which is what the shipped wrapper uses (Winlator's SysV shm, same
#     libandroid_shm* symbols, present in imagefs/usr/lib).
#   * libadrenotools.so is built from leegao/libadrenotools (the fork the wrapper is written against)
#     with the NDK's CMake, only to link against; at run time the imagefs copy is used, as today.
#   * RUNPATH $ORIGIN (was the Termux prefix).
#
# Environment (all optional): SPIRV_TOOLS_REF, SPIRV_HEADERS_REF, ADRENOTOOLS_REF, OUT_DIR, WITH_AHB=1
# (apply Banners-Turnip's banner_ahb_wsi.py zero-copy patch; needs BANNERS_TURNIP_DIR).

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
 libx11 libxcb libxau libxdmcp xorgproto libxrandr libxrender libxext libxfixes libxshmfence zlib zstd"
sysroot="$work/termux"
tprefix="$sysroot/data/data/com.termux/files/usr"
build="$work/build"

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
		-DCMAKE_BUILD_TYPE=Release || die "cmake configure libadrenotools failed"
	cmake --build "$work/libadrenotools/build" --target adrenotools || die "libadrenotools build failed"
	mkdir -p "$work/adrenotools-lib"
	cp -L "$work/libadrenotools/build/libadrenotools.so" "$work/adrenotools-lib/" || die "libadrenotools.so not produced"
	"$ndk/llvm-readelf" --dyn-syms -W "$work/adrenotools-lib/libadrenotools.so" | grep -q ' adrenotools_open_libvulkan$' \
		|| die "built libadrenotools.so does not export adrenotools_open_libvulkan"
	log "libadrenotools SONAME: $("$ndk/llvm-readelf" -d "$work/adrenotools-lib/libadrenotools.so" | grep SONAME || echo none)"
}

apply_ahb(){
	[ "${WITH_AHB:-0}" = 1 ] || { log "banner_ahb_wsi: not requested"; return 0; }
	[ -n "$BANNERS_TURNIP_DIR" ] || die "WITH_AHB=1 needs BANNERS_TURNIP_DIR"
	log "applying banner_ahb_wsi.py"
	python3 "$BANNERS_TURNIP_DIR/patches/wayland/banner_ahb_wsi.py" "$repo" || die "banner_ahb_wsi.py did not apply"
}

configure_build(){
	cd "$repo"
	command -v glslangValidator >/dev/null || die "glslangValidator missing"
	pkg-config --exists wayland-scanner || die "native wayland-scanner.pc missing (apt libwayland-dev)"
	log "native wayland-scanner $(pkg-config --modversion wayland-scanner)"

	local inc="-I$tprefix/include"
	local defs="-D__TERMUX__ -D__USE_GNU -D__ANDROID__"
	local warn="-Wno-error -Wno-deprecated-declarations -Wno-incompatible-pointer-types -Wno-incompatible-pointer-types-discards-qualifiers -Wno-int-conversion"
	local libs="-L$tprefix/lib -L$work/adrenotools-lib -landroid-shmem -ladrenotools"
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
pkg_config_libdir = ['$tprefix/lib/pkgconfig', '$tprefix/share/pkgconfig']

[built-in options]
c_args = [$(for f in $inc $defs $warn; do printf "'%s', " "$f"; done)]
cpp_args = [$(for f in $inc $defs $warn; do printf "'%s', " "$f"; done)]
c_link_args = [$(for f in $libs; do printf "'%s', " "$f"; done)]
cpp_link_args = [$(for f in $libs -static-libstdc++ -Wl,--exclude-libs,ALL; do printf "'%s', " "$f"; done)]

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
		-Dcpp_rtti=false -Dgbm=disabled -Dopengl=false -Dllvm=disabled -Dshared-llvm=disabled \
		-Dplatforms=x11,wayland -Dgallium-drivers= -Dxmlconfig=disabled -Dvulkan-drivers=wrapper \
		|| { cat "$build/meson-logs/meson-log.txt" | tail -80; die "meson setup failed"; }
	ninja -C "$build" src/vulkan/wrapper/libvulkan_wrapper.so || die "ninja failed"
	[ -f "$build/src/vulkan/wrapper/libvulkan_wrapper.so" ] || die "libvulkan_wrapper.so not built"
}

package_check(){
	cd "$repo"
	local re="$ndk/llvm-readelf" so="$out/libvulkan_wrapper.so"
	cp -L "$build/src/vulkan/wrapper/libvulkan_wrapper.so" "$so"
	patchelf --replace-needed libandroid-shmem.so libandroid-sysvshm.so "$so" || die "patchelf replace-needed failed"
	patchelf --set-rpath '$ORIGIN' "$so" || die "patchelf set-rpath failed"

	local dyn syms needed
	dyn="$("$re" -d "$so")"; syms="$("$re" --dyn-syms -W "$so")"
	needed="$(echo "$dyn" | grep -oP 'NEEDED.*\[\K[^]]+' | tr '\n' ' ')"
	log "NEEDED: $needed"
	log "SONAME: $(echo "$dyn" | grep -oP 'SONAME.*\[\K[^]]+')"
	log "RUNPATH: $(echo "$dyn" | grep -E 'RUNPATH|RPATH' || echo none)"
	echo "$dyn" | grep -q 'SONAME.*\[libvulkan_wrapper.so\]' || die "SONAME is not libvulkan_wrapper.so"
	for s in vk_icdGetInstanceProcAddr vk_icdNegotiateLoaderICDInterfaceVersion vk_icdGetPhysicalDeviceProcAddr; do
		echo "$syms" | grep -qE "FUNC +GLOBAL +DEFAULT +[0-9]+ $s\$" || die "$s not exported"
	done
	# Every NEEDED lib must exist where the guest looks: imagefs/usr/lib (list taken from the device
	# 2026-09-28), the Android system libs, or libwayland-client.so from the Proton wcp's lib/ (which
	# GuestProgramLauncherComponent puts first on LD_LIBRARY_PATH in Wayland mode).
	local ok="libc.so libm.so libdl.so liblog.so libnativewindow.so libandroid.so libsync.so
		libandroid-sysvshm.so libadrenotools.so libdrm.so libxcb.so libX11-xcb.so libX11.so libxcb-dri3.so
		libxcb-present.so libxcb-sync.so libxcb-randr.so libxcb-shm.so libxcb-xfixes.so libxshmfence.so
		libz.so libzstd.so libwayland-client.so libffi.so"
	local n
	for n in $needed; do
		echo " $ok " | tr -s ' \t\n' ' ' | grep -q " $n " || die "NEEDED $n is not in imagefs/usr/lib, the system, or the Proton lib/"
	done
	echo " $needed " | grep -q ' libwayland-client.so ' || die "no NEEDED libwayland-client.so: the Wayland WSI is not in"
	echo " $needed " | grep -q ' libadrenotools.so ' || die "no NEEDED libadrenotools.so"
	echo " $needed " | grep -q ' libc++_shared.so ' && die "libc++_shared.so is NEEDED (expected static libc++)"
	echo "$syms" | grep -q ' UND .*adrenotools_open_libvulkan' || die "adrenotools_open_libvulkan not imported"
	local wl; wl="$(echo "$syms" | grep -c ' UND .*wl_' || true)"
	[ "$wl" -gt 10 ] || die "only $wl wl_* imports: the Wayland WSI is not in"
	grep -q "VK_KHR_wayland_surface" "$so" || die "VK_KHR_wayland_surface string missing"
	grep -q "VK_KHR_xcb_surface" "$so" || die "VK_KHR_xcb_surface string missing (X11 WSI dropped?)"
	# Symbols only a newer libwayland-client has (1.23: queue names, dispatch timeout, proxy queue;
	# 1.24: wl_fixes). Any of them imported = will not load against an older libwayland-client.
	local sym
	for sym in wl_display_dispatch_queue_timeout wl_display_create_queue_with_name wl_fixes_interface \
	           wl_proxy_get_queue wl_event_queue_get_name; do
		echo "$syms" | grep -qE " UND +$sym\$" && die "imports $sym: will not load against an older libwayland-client"
	done
	# The fallbacks must not leak out of the .so either.
	echo "$syms" | grep -vE ' UND ' | grep -qE ' (wl_display_dispatch_queue_timeout|wl_display_create_queue_with_name)$' \
		&& die "exports a libwayland symbol (fallback not hidden)"
	echo "$syms" | grep -E ' UND .*wl_' | awk '{print $NF}' | sort > "$out/wl-imports.txt"
	echo "$syms" | grep -vE ' UND ' | grep -E 'FUNC|OBJECT' | grep GLOBAL | awk '{print $NF}' | sort > "$out/exports.txt"
	log "$wl wl_* imports: $(tr '\n' ' ' < "$out/wl-imports.txt")"
	log "exports: $(tr '\n' ' ' < "$out/exports.txt")"
	if [ "${WITH_AHB:-0}" = 1 ]; then
		grep -q banner_ahb_v1 "$so" || die "banner_ahb_v1 missing (zero-copy patch not in)"
		log "banner_ahb_v1: in"
	fi
	echo "$dyn" > "$out/dynamic.txt"

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
}

prepare
build_spirv_tools
build_adrenotools
apply_ahb
configure_build
package_check
log "done: $out/libvulkan_wrapper.so"
