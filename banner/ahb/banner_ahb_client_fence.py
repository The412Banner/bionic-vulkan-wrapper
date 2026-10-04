#!/usr/bin/env python3
"""
Bannerlator zero-copy layers, part 2: the client's render-complete fence per presented buffer
(banner_ahb_v1.fence, interface version 3). Applied AFTER banner_ahb_wsi_mesa242.py (it extends the
state that script adds; its anchors are asserted, so a mismatch fails the build).

Usage: banner_ahb_client_fence.py <Mesa root>

Why. The compositor puts a gralloc swapchain image on a SurfaceControl layer and needs an acquire fence
for every commit. Until now it exported one from the dma-buf itself (DMA_BUF_IOCTL_EXPORT_SYNC_FILE):
that is every fence the dma-buf carries, not just the render-complete point, and it costs the compositor
a kernel round-trip per frame. The client knows the exact fence: Mesa's wsi_common_queue_present()
submits an empty batch on the program's queue that waits on the present's wait semaphores and signals
chain->dma_buf_semaphore (an exportable SYNC_FD semaphore), and wsi_signal_dma_buf_from_semaphore()
exports that as a sync_file to import it into the dma-buf. A dup of that sync_file IS the acquire fence.

What.
  * struct wsi_image gets banner_render_fence_fd (-1 by default, closed with the image);
    struct wsi_swapchain gets banner_client_fence (the chain sends fences).
  * wsi_signal_dma_buf_from_semaphore() keeps a dup of the sync_file on the image (the import into the
    dma-buf is unchanged) when the chain sends fences.
  * wsi_wl_swapchain_queue_present() sends banner_ahb_v1.fence(buffer, fd) right before
    wl_surface_attach when the chain is a gralloc (banner_ahb mode) chain and the compositor's
    banner_ahb_v1 is version >= 3, then closes its copy (libwayland dups fds on send). Without a stashed
    fd (no exportable semaphore on this chain) it exports the dma-buf's write fences
    (DMA_BUF_IOCTL_EXPORT_SYNC_FILE with DMA_BUF_SYNC_READ: what a reader of the buffer must wait for)
    and sends that; if that fails too, nothing is sent (today's behaviour, the compositor exports).
  * BANNER_WSI_NO_CLIENT_FENCE=1 turns the sending off. One stderr line per gralloc swapchain:
    "banner-ahb: client render fences on (sync_fd from semaphore|dma-buf export)" at the first fence,
    or "banner-ahb: client render fences off (<reason>)" when the chain is set up.
Version 1 / 2 compositors, non-gralloc chains and the X11 WSI are untouched.
"""
import os
import sys

mesa = sys.argv[1]
wsi = os.path.join(mesa, 'src/vulkan/wsi')


def patch(path, edits):
    s = open(path).read()
    for old, new, count in edits:
        n = s.count(old)
        assert n == count, "%s: expected %d of %r, found %d" % (path, count, old[:70], n)
        s = s.replace(old, new)
    open(path, 'w').write(s)


# 1. State: the fence on the image, the switch on the chain.
patch(os.path.join(wsi, 'wsi_common_private.h'), [
    ('   int dma_buf_fd;\n#endif\n   void *cpu_map;\n',
     '   int dma_buf_fd;\n'
     '   int banner_render_fence_fd; /* Bannerlator: dup of the last render-complete sync_file, -1 when none */\n'
     '#endif\n   void *cpu_map;\n', 1),
    ('   bool banner_wait_dma_buf; /* Bannerlator: acquire imports the dma-buf fences (wrapper, Wayland) */\n',
     '   bool banner_wait_dma_buf; /* Bannerlator: acquire imports the dma-buf fences (wrapper, Wayland) */\n'
     '   bool banner_client_fence; /* Bannerlator: send the render-complete sync_file as banner_ahb_v1.fence */\n', 1),
])

patch(os.path.join(wsi, 'wsi_common.c'), [
    ('   image->dma_buf_fd = -1;\n',
     '   image->dma_buf_fd = -1;\n   image->banner_render_fence_fd = -1;\n', 1),
    ('   if (image->dma_buf_fd >= 0)\n      close(image->dma_buf_fd);\n',
     '   if (image->dma_buf_fd >= 0)\n      close(image->dma_buf_fd);\n'
     '   if (image->banner_render_fence_fd >= 0)\n      close(image->banner_render_fence_fd);\n', 1),
])

# 2. Keep a copy of the render-complete sync_file where Mesa exports it.
patch(os.path.join(wsi, 'wsi_common_drm.c'), [
    ('   result = wsi_dma_buf_import_sync_file(image->dma_buf_fd, sync_file_fd);\n'
     '   close(sync_file_fd);\n'
     '   return result;\n',
     '   /* Bannerlator: this sync_file is exactly "rendering into the image is complete"; a gralloc chain\n'
     '    * hands a copy to the compositor as the commit\'s acquire fence (banner_ahb_v1.fence, see\n'
     '    * banner_cf_send in wsi_common_wayland.c). The import below is unchanged. */\n'
     '   if (chain->banner_client_fence) {\n'
     '      struct wsi_image *banner_image = (struct wsi_image *)image;\n'
     '      if (banner_image->banner_render_fence_fd >= 0)\n'
     '         close(banner_image->banner_render_fence_fd);\n'
     '      banner_image->banner_render_fence_fd = os_dupfd_cloexec(sync_file_fd);\n'
     '   }\n'
     '   result = wsi_dma_buf_import_sync_file(image->dma_buf_fd, sync_file_fd);\n'
     '   close(sync_file_fd);\n'
     '   return result;\n', 1),
])

# 3. The Wayland side: decide per chain, send per present.
helpers = r'''
/* ---- Bannerlator client render fences (banner_ahb_v1.fence, interface version 3) --------------
 * banner_ahb_client_fence.py has the design. Only a gralloc (banner.mode) chain on a version >= 3
 * compositor sends; BANNER_WSI_NO_CLIENT_FENCE=1 turns it off. */
#include <errno.h>
#include <sys/ioctl.h>
struct banner_cf_export_sync_file { uint32_t flags; int32_t fd; };
#define BANNER_CF_DMA_BUF_SYNC_READ 1u /* export the write fences: what a reader must wait for */
#define BANNER_CF_DMA_BUF_IOCTL_EXPORT_SYNC_FILE _IOWR('b', 2, struct banner_cf_export_sync_file)
static int banner_cf_export_state; /* 0 = untried, 1 = works, -1 = the kernel has no ioctl */

static void
banner_cf_setup_chain(struct wsi_wl_swapchain *chain)
{
   const struct wsi_wl_display *display = chain->wsi_wl_surface->display;
   chain->base.banner_client_fence = false;
   if (!chain->banner.mode)
      return;
   const char *e = getenv("BANNER_WSI_NO_CLIENT_FENCE");
   if (e && e[0] == '1') {
      fprintf(stderr, "banner-ahb: client render fences off (BANNER_WSI_NO_CLIENT_FENCE=1)\n");
      return;
   }
   if (!display->banner_ahb || display->banner_ahb_version < BANNER_AHB_V1_FENCE_SINCE_VERSION) {
      fprintf(stderr, "banner-ahb: client render fences off (compositor banner_ahb_v1 version %u, fence needs %u)\n",
              display->banner_ahb_version, (unsigned)BANNER_AHB_V1_FENCE_SINCE_VERSION);
      return;
   }
   chain->base.banner_client_fence = true;
}

/* Right before wl_surface_attach: the render-complete fence for this commit. The fd comes from
 * wsi_signal_dma_buf_from_semaphore (a dup of the sync_file Mesa imports into the dma-buf); a chain
 * without an exportable semaphore falls back to exporting the dma-buf's write fences. libwayland dups
 * the fd on send, so our copy is closed here either way. */
static void
banner_cf_send(struct wsi_wl_swapchain *chain, struct wsi_wl_image *image)
{
   if (!chain->base.banner_client_fence || !image->buffer)
      return;
   int fd = image->base.banner_render_fence_fd;
   const char *source = "sync_fd from semaphore";
   image->base.banner_render_fence_fd = -1;
   if (fd < 0) {
      if (image->base.dma_buf_fd < 0 || banner_cf_export_state < 0)
         return;
      struct banner_cf_export_sync_file exp = {.flags = BANNER_CF_DMA_BUF_SYNC_READ, .fd = -1};
      int r;
      do {
         r = ioctl(image->base.dma_buf_fd, BANNER_CF_DMA_BUF_IOCTL_EXPORT_SYNC_FILE, &exp);
      } while (r < 0 && (errno == EINTR || errno == EAGAIN));
      if (r < 0 || exp.fd < 0) {
         if (errno == ENOTTY || errno == ENOSYS || errno == EINVAL) {
            banner_cf_export_state = -1;
            fprintf(stderr, "banner-ahb: client render fences off (DMA_BUF_IOCTL_EXPORT_SYNC_FILE unsupported: %s)\n",
                    strerror(errno));
         }
         return;
      }
      banner_cf_export_state = 1;
      fd = exp.fd;
      source = "dma-buf export";
   }
   if (!chain->banner.fence_said) {
      chain->banner.fence_said = true;
      fprintf(stderr, "banner-ahb: client render fences on (%s)\n", source);
   }
   banner_ahb_v1_fence(chain->wsi_wl_surface->display->banner_ahb, image->buffer, fd);
   close(fd);
}
/* ---- end Bannerlator client render fences ------------------------------------------------------ */
'''

patch(os.path.join(wsi, 'wsi_common_wayland.c'), [
    # chain state: the once-only log flag, next to the ones banner_ahb_wsi_mesa242.py added
    ('      bool held_said;             /* the "every free image is held" line was logged once */\n',
     '      bool held_said;             /* the "every free image is held" line was logged once */\n'
     '      bool fence_said;            /* the "client render fences on" line was logged once */\n', 1),
    # helpers, after the zero-copy block
    ('/* ---- end Bannerlator zero-copy layers ------------------------------------------------------- */\n',
     '/* ---- end Bannerlator zero-copy layers ------------------------------------------------------- */\n' + helpers, 1),
    # decide once the chain knows whether it is a gralloc chain
    ('   banner_ahb_setup_chain(chain);\n',
     '   banner_ahb_setup_chain(chain);\n   banner_cf_setup_chain(chain);\n', 1),
    # the fence for this commit, before the attach
    ('   assert(image_index < chain->base.image_count);\n'
     '   wl_surface_attach(wsi_wl_surface->surface, chain->images[image_index].buffer, 0, 0);\n',
     '   assert(image_index < chain->base.image_count);\n'
     '   banner_cf_send(chain, &chain->images[image_index]); /* Bannerlator: render-complete fence for this commit */\n'
     '   wl_surface_attach(wsi_wl_surface->surface, chain->images[image_index].buffer, 0, 0);\n', 1),
])

print("wsi: client render fences (banner_ahb_v1.fence v3, BANNER_WSI_NO_CLIENT_FENCE) applied")
