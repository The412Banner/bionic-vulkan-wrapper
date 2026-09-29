/*
 * Bannerlator: make the KGSL driver's zero-timeout timestamp waits real polls.
 *
 * Why. Turnip's KGSL backend waits for a GPU timestamp with IOCTL_KGSL_DEVICE_WAITTIMESTAMP_CTXTID
 * and a timeout in milliseconds (tu_knl_kgsl.cc wait_timestamp_safe(), get_relative_ms()). A caller
 * that only wants to know "is it done yet?" passes abs_timeout 0, which becomes timeout 0 -- but the
 * KGSL kernel driver reads timeout 0 as "wait forever" (adreno_drawctxt_wait(): "If timeout is 0,
 * wait forever"). The caller that hits it every frame is Mesa's emulated timeline semaphore
 * (vk_sync_timeline_gc_locked(), called with the timeline mutex held from vk_sync_timeline_alloc_point()
 * on every vkQueueSubmit that signals a timeline, and from every timeline wait): the "poll" blocks until
 * the oldest pending point retires on the GPU, with the mutex held. vkd3d-proton signals timelines on
 * every submission, so its submission thread sleeps inside vkQueueSubmit2 until the previous frame is
 * done and its fence thread holds the same mutex while it sleeps: CPU and GPU never overlap. Device
 * profile 2026-09-29 (Pocket FIT, D3D12 demo on the Wayland adapter): the vkd3d_queue thread spends
 * 91 % of its time inside the driver's vkQueueSubmit2 (63 % on that mutex, 25 % in
 * kgsl waittimestamp with timeout 0); ~614 fps at 77 % GPU.
 *
 * What. Right after the driver is loaded, its GOT slot for ioctl() is pointed at banner_kgsl_ioctl(),
 * which turns exactly one case -- WAITTIMESTAMP_CTXTID with timeout 0 -- into
 * IOCTL_KGSL_CMDSTREAM_READTIMESTAMP_CTXTID (RETIRED) + a wrap-safe compare: 0 if retired, else -1 /
 * ETIMEDOUT, which Turnip already maps to VK_TIMEOUT (what the caller asked for). Every other ioctl, and
 * this one with any non-zero timeout (Turnip's "forever" is 0xffffffff), goes to the previous target
 * unchanged. If the read fails the original ioctl runs (old behaviour). Only the driver this wrapper
 * loads is touched (the X11 wrapper in imagefs is a different library). BANNER_KGSL_POLL_FIX=0 = off.
 */
#include <dlfcn.h>
#include <elf.h>
#include <errno.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#ifndef R_AARCH64_GLOB_DAT
#define R_AARCH64_GLOB_DAT 1025
#endif
#ifndef R_AARCH64_JUMP_SLOT
#define R_AARCH64_JUMP_SLOT 1026
#endif

#define BANNER_KGSL_WAITTIMESTAMP_CTXTID 0x400c0907u /* _IOW(0x09, 0x07, 12 bytes) */
#define BANNER_KGSL_READTIMESTAMP_CTXTID 0xc00c0916u /* _IOWR(0x09, 0x16, 12 bytes) */
#define BANNER_KGSL_TIMESTAMP_RETIRED 2u

struct banner_kgsl_waitts { uint32_t context_id, timestamp, timeout; };
struct banner_kgsl_readts { uint32_t context_id, type, timestamp; };

typedef int (*banner_ioctl_fn)(int, int, ...);
static banner_ioctl_fn banner_kgsl_next_ioctl;
static int banner_kgsl_said_poll;

static int
banner_kgsl_ioctl(int fd, int request, void *arg)
{
   if ((uint32_t)request == BANNER_KGSL_WAITTIMESTAMP_CTXTID && arg &&
       ((const struct banner_kgsl_waitts *)arg)->timeout == 0) {
      const struct banner_kgsl_waitts *w = arg;
      struct banner_kgsl_readts r = {.context_id = w->context_id, .type = BANNER_KGSL_TIMESTAMP_RETIRED};
      if (banner_kgsl_next_ioctl(fd, (int)BANNER_KGSL_READTIMESTAMP_CTXTID, &r) == 0) {
         if (!__atomic_exchange_n(&banner_kgsl_said_poll, 1, __ATOMIC_RELAXED))
            fprintf(stderr, "wrapper-kgsl: zero-timeout timestamp wait answered as a poll (first one: ctx %u ts %u retired %u)\n",
                    w->context_id, w->timestamp, r.timestamp);
         if ((int32_t)(r.timestamp - w->timestamp) >= 0)
            return 0;
         errno = ETIMEDOUT;
         return -1;
      }
   }
   return banner_kgsl_next_ioctl(fd, request, arg);
}

/* Point the ioctl GOT slot(s) of the ELF object containing `inside` at banner_kgsl_ioctl. */
static int
banner_kgsl_hook_object(const void *inside)
{
   Dl_info info;
   if (!inside || !dladdr(inside, &info) || !info.dli_fbase)
      return -1;
   const uint8_t *base = info.dli_fbase;
   const ElfW(Ehdr) *eh = (const ElfW(Ehdr) *)base;
   if (memcmp(eh->e_ident, ELFMAG, SELFMAG) != 0 || eh->e_ident[EI_CLASS] != ELFCLASS64)
      return -1;
   const ElfW(Phdr) *ph = (const ElfW(Phdr) *)(base + eh->e_phoff);
   ElfW(Addr) first_load = (ElfW(Addr))-1;
   const ElfW(Phdr) *dynph = NULL, *relro = NULL;
   for (int i = 0; i < eh->e_phnum; i++) {
      if (ph[i].p_type == PT_GNU_RELRO)
         relro = &ph[i];
      if (ph[i].p_type == PT_LOAD && ph[i].p_vaddr < first_load)
         first_load = ph[i].p_vaddr & ~(ElfW(Addr))0xfff;
      if (ph[i].p_type == PT_DYNAMIC)
         dynph = &ph[i];
   }
   if (!dynph || first_load == (ElfW(Addr))-1)
      return -1;
   const uintptr_t bias = (uintptr_t)base - first_load;
#define BANNER_PTR(v) ((uintptr_t)(v) < bias ? bias + (uintptr_t)(v) : (uintptr_t)(v))
   const ElfW(Dyn) *dyn = (const ElfW(Dyn) *)(bias + dynph->p_vaddr);
   const ElfW(Sym) *symtab = NULL;
   const char *strtab = NULL;
   const ElfW(Rela) *rel[2] = {NULL, NULL};
   size_t relsz[2] = {0, 0};
   for (; dyn->d_tag != DT_NULL; dyn++) {
      switch (dyn->d_tag) {
      case DT_SYMTAB: symtab = (const ElfW(Sym) *)BANNER_PTR(dyn->d_un.d_ptr); break;
      case DT_STRTAB: strtab = (const char *)BANNER_PTR(dyn->d_un.d_ptr); break;
      case DT_JMPREL: rel[0] = (const ElfW(Rela) *)BANNER_PTR(dyn->d_un.d_ptr); break;
      case DT_PLTRELSZ: relsz[0] = dyn->d_un.d_val; break;
      case DT_RELA: rel[1] = (const ElfW(Rela) *)BANNER_PTR(dyn->d_un.d_ptr); break;
      case DT_RELASZ: relsz[1] = dyn->d_un.d_val; break;
      }
   }
#undef BANNER_PTR
   if (!symtab || !strtab)
      return -1;
   int hooked = 0;
   const long page = sysconf(_SC_PAGESIZE);
   for (int t = 0; t < 2; t++) {
      if (!rel[t])
         continue;
      for (size_t i = 0; i < relsz[t] / sizeof(ElfW(Rela)); i++) {
         const uint32_t type = ELF64_R_TYPE(rel[t][i].r_info);
         if (type != R_AARCH64_JUMP_SLOT && type != R_AARCH64_GLOB_DAT)
            continue;
         const uint32_t si = ELF64_R_SYM(rel[t][i].r_info);
         if (!si || strcmp(strtab + symtab[si].st_name, "ioctl") != 0)
            continue;
         void **slot = (void **)(bias + rel[t][i].r_offset);
         if (*slot == (void *)banner_kgsl_ioctl)
            continue;
         uintptr_t pg = (uintptr_t)slot & ~(uintptr_t)(page - 1);
         if (mprotect((void *)pg, (size_t)page, PROT_READ | PROT_WRITE) != 0)
            continue;
         if (!banner_kgsl_next_ioctl)
            banner_kgsl_next_ioctl = (banner_ioctl_fn)*slot;
         __atomic_store_n(slot, (void *)banner_kgsl_ioctl, __ATOMIC_RELEASE);
         /* Back to read-only only inside RELRO (where the linker left it); elsewhere it was writable. */
         if (relro && (uintptr_t)slot >= bias + relro->p_vaddr &&
             (uintptr_t)slot < bias + relro->p_vaddr + relro->p_memsz)
            mprotect((void *)pg, (size_t)page, PROT_READ);
         hooked++;
      }
   }
   return hooked;
}

struct banner_kgsl_find { const char *name; uintptr_t self; const char *found; const void *addr; };

static int
banner_kgsl_find_cb(struct dl_phdr_info *info, size_t size, void *data)
{
   struct banner_kgsl_find *f = data;
   (void)size;
   if (!info->dlpi_name || (uintptr_t)info->dlpi_addr == f->self)
      return 0;
   const char *slash = strrchr(info->dlpi_name, '/');
   if (strcmp(slash ? slash + 1 : info->dlpi_name, f->name) != 0)
      return 0;
   for (int i = 0; i < info->dlpi_phnum; i++) {
      if (info->dlpi_phdr[i].p_type == PT_LOAD && (info->dlpi_phdr[i].p_flags & PF_X)) {
         f->found = info->dlpi_name;
         f->addr = (const void *)(info->dlpi_addr + info->dlpi_phdr[i].p_vaddr);
         return 1;
      }
   }
   return 0;
}

/* Called once the driver underneath is known to be Turnip (driverID) and loaded. The adrenotools
 * handle is the system Vulkan loader, not the driver, so the driver is found among the loaded objects by
 * ADRENOTOOLS_DRIVER_NAME (this wrapper itself excluded: an imported copy of it has that name too). */
static void
banner_kgsl_poll_fix(void)
{
   static int done;
   if (__atomic_exchange_n(&done, 1, __ATOMIC_ACQ_REL))
      return;
   const char *e = getenv("BANNER_KGSL_POLL_FIX");
   if (e && e[0] == '0') {
      fprintf(stderr, "wrapper-kgsl: zero-timeout poll fix off (BANNER_KGSL_POLL_FIX=0)\n");
      return;
   }
   const char *name = getenv("ADRENOTOOLS_DRIVER_NAME");
   if (!name || !name[0]) {
      fprintf(stderr, "wrapper-kgsl: zero-timeout poll fix not applied (no ADRENOTOOLS_DRIVER_NAME)\n");
      return;
   }
   Dl_info self;
   struct banner_kgsl_find f = {.name = name};
   if (dladdr((const void *)banner_kgsl_poll_fix, &self) && self.dli_fbase) {
      /* dlpi_addr is the load bias; this library's first PT_LOAD is at vaddr 0, so bias == base. */
      f.self = (uintptr_t)self.dli_fbase;
   }
   dl_iterate_phdr(banner_kgsl_find_cb, &f);
   if (!f.addr) {
      fprintf(stderr, "wrapper-kgsl: zero-timeout poll fix not applied (%s not loaded)\n", name);
      return;
   }
   int n = banner_kgsl_hook_object(f.addr);
   fprintf(stderr, "wrapper-kgsl: zero-timeout poll fix %s (%s: %d ioctl slot%s)\n",
           n > 0 ? "on" : "not applied", f.found, n > 0 ? n : 0, n == 1 ? "" : "s");
}
