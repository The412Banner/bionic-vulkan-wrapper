/*
 * Bannerlator: Mali (PanVK) compatibility switches for the Wayland adapter. All OFF by default; with
 * none set the wrapper behaves exactly as before.
 *
 * Ported from FristOneRR-Wrapperv1 (https://github.com/FristOneRR-Admin/FristOneRR-Wrapperv1, MIT,
 * commit 834d827e "wrapper: FristOneRR-Wrapperv1 changes for FristOneRR PanVK + DXVK 2.x"), which
 * makes DXVK 2.x run on FristOneRR's PanVK (Mesa PanVK on kbase, Mali-G57). There they are always on;
 * here each one is a runtime switch:
 *
 *   BANNER_MALI_HIDE_EXTS=1         VK_KHR_present_id, VK_KHR_present_wait and the VK_KHR_dynamic_rendering
 *                                   EXTENSION are not exposed (presentId / presentWait features off too;
 *                                   Vulkan 1.3 core dynamic rendering stays). [wrapper_physical_device.c]
 *   BANNER_MALI_NO_SUBMIT_WAITS=1   vkQueueSubmit / vkQueueSubmit2 drop every wait semaphore (binary and
 *                                   timeline). Safe ONLY on a driver that runs one queue strictly in order.
 *                                   The WSI's own present submit goes through the same entry point, so it
 *                                   loses its wait on the program's render semaphore too, as in FristOneRR's
 *                                   build. [wrapper_device.c]
 *   BANNER_MALI_NO_ACQUIRE_SIGNAL=1 vkAcquireNextImage(2)KHR does not signal the program's acquire semaphore
 *                                   or fence. Honoured only together with BANNER_MALI_NO_SUBMIT_WAITS=1:
 *                                   alone, the program's next submit would wait on a binary semaphore nothing
 *                                   ever signals (GPU hang). A program that waits on the acquire FENCE on the
 *                                   CPU still hangs (DXVK passes no fence). On the Wayland dma-buf / gralloc
 *                                   chains this also skips the tear-safe wait for the compositor / display
 *                                   (banner_wait_dma_buf), so the game may draw into a buffer still on screen.
 *                                   [wsi_common.c]
 *
 * Read once, from a constructor in the one translation unit that defines BANNER_MALI_IMPL
 * (wrapper_physical_device.c); everything else reads banner_mali_flags. One log line when any is on.
 */
#ifndef BANNER_MALI_H
#define BANNER_MALI_H

#define BANNER_MALI_HIDE_EXTS         (1u << 0)
#define BANNER_MALI_NO_SUBMIT_WAITS   (1u << 1)
#define BANNER_MALI_NO_ACQUIRE_SIGNAL (1u << 2)

extern unsigned banner_mali_flags __attribute__((visibility("hidden")));

#ifdef BANNER_MALI_IMPL
#include <stdio.h>
#include <stdlib.h>

unsigned banner_mali_flags __attribute__((visibility("hidden")));

static int
banner_mali_env(const char *name)
{
   const char *v = getenv(name);
   return v && v[0] == '1';
}

__attribute__((constructor)) static void
banner_mali_init(void)
{
   unsigned f = 0;
   if (banner_mali_env("BANNER_MALI_HIDE_EXTS"))
      f |= BANNER_MALI_HIDE_EXTS;
   if (banner_mali_env("BANNER_MALI_NO_SUBMIT_WAITS"))
      f |= BANNER_MALI_NO_SUBMIT_WAITS;
   int want_no_acquire = banner_mali_env("BANNER_MALI_NO_ACQUIRE_SIGNAL");
   if (want_no_acquire && (f & BANNER_MALI_NO_SUBMIT_WAITS))
      f |= BANNER_MALI_NO_ACQUIRE_SIGNAL;
   banner_mali_flags = f;
   if (f || want_no_acquire)
      fprintf(stderr, "wrapper-mali: hide_exts=%d no_submit_waits=%d no_acquire_signal=%d%s\n",
              !!(f & BANNER_MALI_HIDE_EXTS), !!(f & BANNER_MALI_NO_SUBMIT_WAITS),
              !!(f & BANNER_MALI_NO_ACQUIRE_SIGNAL),
              want_no_acquire && !(f & BANNER_MALI_NO_ACQUIRE_SIGNAL)
                 ? " (BANNER_MALI_NO_ACQUIRE_SIGNAL ignored: needs BANNER_MALI_NO_SUBMIT_WAITS=1, else the "
                   "next submit waits on a semaphore nothing signals)"
                 : "");
}
#endif

#endif
