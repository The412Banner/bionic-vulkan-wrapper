/* Bannerlator: pass-through dispatch for the bionic Vulkan wrapper. The design is in banner_passthrough.h. */
#include "banner_passthrough.h"
#include "wrapper_trampolines.h"
#include "vk_dispatch_table.h"
#include "util/hash_table.h"
#include "util/simple_mtx.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct banner_pt_cmd {
   struct wrapper_device *device;
   VkCommandPool pool;
};

static simple_mtx_t banner_pt_mtx = SIMPLE_MTX_INITIALIZER;
static struct hash_table_u64 *banner_pt_cmds;                /* driver VkCommandBuffer -> banner_pt_cmd */
static struct vk_device_dispatch_table banner_pt_cmd_table;  /* VkCommandBuffer entry points only */
static bool banner_pt_cmd_table_ready;
static int banner_pt_env = -1;

bool
banner_pt_enabled(void)
{
   if (banner_pt_env < 0) {
      const char *e = getenv("BANNER_WRAPPER_PASSTHROUGH");
      banner_pt_env = (e && e[0] == '0') ? 0 : 1;
   }
   return banner_pt_env == 1;
}

/* A dispatch table whose only non-NULL slots are the VkCommandBuffer entry points (the generated
 * side-table trampolines): the classifier for "is this name a command-buffer function". */
static const struct vk_device_dispatch_table *
banner_pt_cmd_dispatch_table(void)
{
   if (!__atomic_load_n(&banner_pt_cmd_table_ready, __ATOMIC_ACQUIRE)) {
      simple_mtx_lock(&banner_pt_mtx);
      if (!banner_pt_cmd_table_ready) {
         vk_device_dispatch_table_from_entrypoints(&banner_pt_cmd_table,
                                                   &banner_pt_command_buffer_trampolines, true);
         __atomic_store_n(&banner_pt_cmd_table_ready, true, __ATOMIC_RELEASE);
      }
      simple_mtx_unlock(&banner_pt_mtx);
   }
   return &banner_pt_cmd_table;
}

PFN_vkVoidFunction
banner_pt_instance_proc_addr(PFN_vkVoidFunction func, const char *name)
{
   if (!func || !name || !banner_pt_enabled())
      return func;
   PFN_vkVoidFunction pt = vk_device_dispatch_table_get(banner_pt_cmd_dispatch_table(), name);
   return pt ? pt : func;
}

PFN_vkVoidFunction
banner_pt_get_device_proc_addr(struct wrapper_device *device, const char *name)
{
   /* The runtime's lookup keeps the spec's gating (core version, enabled extensions). */
   PFN_vkVoidFunction ours = vk_device_get_proc_addr(&device->vk, name);
   if (!ours || !device->banner_passthrough)
      return ours;
   if (!vk_device_dispatch_table_get(banner_pt_cmd_dispatch_table(), name))
      return ours; /* VkDevice / VkQueue entry point: the handle is wrapped, keep the trampoline */
   /* The application holds the driver's command buffers: hand it the driver's function. NULL when the
    * driver has no such function (the old trampoline would have called a NULL pointer). */
   return vk_device_dispatch_table_get(&device->dispatch_table, name);
}

void
banner_pt_device_init(struct wrapper_device *device)
{
   const char *why = NULL;
   if (!banner_pt_enabled())
      why = "BANNER_WRAPPER_PASSTHROUGH=0";
   else if (device->physical->emulate_bcn > 0)
      why = "BCn emulation is active for this device, so command buffers stay wrapped";
   device->banner_passthrough = (why == NULL);

   /* Count, slot by slot: the wrapper's table against the plain trampolines and the driver's. */
   struct vk_device_dispatch_table tramp;
   vk_device_dispatch_table_from_entrypoints(&tramp, &wrapper_device_trampolines, true);
   const PFN_vkVoidFunction *ours = (const PFN_vkVoidFunction *)&device->vk.dispatch_table;
   const PFN_vkVoidFunction *drv = (const PFN_vkVoidFunction *)&device->dispatch_table;
   const PFN_vkVoidFunction *tr = (const PFN_vkVoidFunction *)&tramp;
   const PFN_vkVoidFunction *cmd = (const PFN_vkVoidFunction *)banner_pt_cmd_dispatch_table();
   const unsigned n = sizeof(tramp) / sizeof(PFN_vkVoidFunction);
   unsigned intercepted = 0, direct = 0, hop = 0, missing = 0;
   for (unsigned i = 0; i < n; i++) {
      if (!ours[i])
         continue;
      if (device->banner_passthrough && cmd[i]) {
         if (drv[i])
            direct++;
         else
            missing++;
         continue;
      }
      if (ours[i] != tr[i])
         intercepted++;
      else
         hop++;
   }
   if (device->banner_passthrough)
      fprintf(stderr, "wrapper-dispatch: pass-through (%u entry points intercepted; %u VkCommandBuffer "
              "entry points are the driver's own%s, %u VkDevice/VkQueue entry points keep the one-hop "
              "trampoline)\n", intercepted, direct,
              missing ? " (some the driver lacks resolve to NULL)" : "", hop);
   else
      fprintf(stderr, "wrapper-dispatch: full wrapping (%s; %u entry points intercepted, %u trampolined)\n",
              why, intercepted, hop);
}

struct wrapper_device *
banner_pt_device_for_cmd(VkCommandBuffer cmd)
{
   struct wrapper_device *device = NULL;
   if (!cmd)
      return NULL;
   simple_mtx_lock(&banner_pt_mtx);
   if (banner_pt_cmds) {
      struct banner_pt_cmd *e = _mesa_hash_table_u64_search(banner_pt_cmds, (uint64_t)(uintptr_t)cmd);
      if (e)
         device = e->device;
   }
   simple_mtx_unlock(&banner_pt_mtx);
   return device;
}

void
banner_pt_track_cmds(struct wrapper_device *device, VkCommandPool pool,
                     uint32_t count, const VkCommandBuffer *cmds)
{
   simple_mtx_lock(&banner_pt_mtx);
   if (!banner_pt_cmds)
      banner_pt_cmds = _mesa_hash_table_u64_create(NULL);
   for (uint32_t i = 0; i < count; i++) {
      if (!cmds[i])
         continue;
      const uint64_t key = (uint64_t)(uintptr_t)cmds[i];
      struct banner_pt_cmd *e = _mesa_hash_table_u64_search(banner_pt_cmds, key);
      if (!e) {
         e = malloc(sizeof(*e));
         if (!e)
            continue;
         _mesa_hash_table_u64_insert(banner_pt_cmds, key, e);
      }
      e->device = device;
      e->pool = pool;
   }
   simple_mtx_unlock(&banner_pt_mtx);
}

void
banner_pt_untrack_cmds(uint32_t count, const VkCommandBuffer *cmds)
{
   simple_mtx_lock(&banner_pt_mtx);
   if (banner_pt_cmds) {
      for (uint32_t i = 0; i < count; i++) {
         if (!cmds[i])
            continue;
         const uint64_t key = (uint64_t)(uintptr_t)cmds[i];
         struct banner_pt_cmd *e = _mesa_hash_table_u64_search(banner_pt_cmds, key);
         if (e) {
            _mesa_hash_table_u64_remove(banner_pt_cmds, key);
            free(e);
         }
      }
   }
   simple_mtx_unlock(&banner_pt_mtx);
}

/* Drop every entry of `device`, or only those of `pool` when pool != VK_NULL_HANDLE. The u64 foreach is
 * safe against deletion of the current entry (util/hash_table.h). */
static void
banner_pt_untrack_matching(struct wrapper_device *device, VkCommandPool pool)
{
   simple_mtx_lock(&banner_pt_mtx);
   if (banner_pt_cmds) {
      hash_table_u64_foreach(banner_pt_cmds, entry) {
         struct banner_pt_cmd *e = entry.data;
         if (e->device != device || (pool != VK_NULL_HANDLE && e->pool != pool))
            continue;
         _mesa_hash_table_u64_remove(banner_pt_cmds, entry.key);
         free(e);
      }
   }
   simple_mtx_unlock(&banner_pt_mtx);
}

void
banner_pt_untrack_pool(struct wrapper_device *device, VkCommandPool pool)
{
   if (pool != VK_NULL_HANDLE)
      banner_pt_untrack_matching(device, pool);
}

void
banner_pt_untrack_device(struct wrapper_device *device)
{
   banner_pt_untrack_matching(device, VK_NULL_HANDLE);
}
