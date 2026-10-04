#!/usr/bin/env python3
"""
Bannerlator: pass-through dispatch for the Pipetto-crypto/mesa wrapper-25 Vulkan wrapper.

Usage: banner_passthrough_patch.py <Mesa root>   (the Pipetto tree; run from anywhere)

Copies banner_passthrough.[ch] next to the wrapper sources and applies anchored edits (every anchor is
asserted, so a Pipetto tree whose wrapper changed shape fails the build instead of shipping a half
patch). The design is in banner_passthrough.h. In short:

  * struct wrapper_device gets `banner_passthrough`; vkCreateDevice decides it (banner_pt_device_init)
    once the driver's dispatch table is loaded and logs one "wrapper-dispatch:" line.
  * vkGetDeviceProcAddr hands out the DRIVER's function for every VkCommandBuffer entry point of a
    pass-through device (the application holds the driver's command buffers), the wrapper's own for
    everything else (VkDevice / VkQueue are wrapped handles).
  * vkAllocateCommandBuffers / vkFreeCommandBuffers / vkDestroyCommandPool / vkDestroyDevice keep the
    driver-handle -> device side table instead of wrapping; vkQueueSubmit / vkQueueSubmit2 forward the
    submits untouched (no command-buffer unwrapping); vkSet/GetPrivateData do not unwrap command buffers.
  * vkGetInstanceProcAddr and the WSI's proc_addr resolve VkCommandBuffer entry points to generated
    side-table trampolines (banner_pt_command_buffer_trampolines, added to the trampolines generator).
  * BANNER_WRAPPER_PASSTHROUGH=0 restores the old dispatch for every device.
"""
import os
import shutil
import sys

mesa = sys.argv[1]
here = os.path.dirname(os.path.abspath(__file__))
wrapper = os.path.join(mesa, 'src/vulkan/wrapper')


def patch(path, edits):
    s = open(path).read()
    for old, new, count in edits:
        n = s.count(old)
        assert n == count, "%s: expected %d of %r, found %d" % (path, count, old[:70], n)
        s = s.replace(old, new)
    open(path, 'w').write(s)


# 0. The implementation, next to the wrapper sources, compiled with them.
for f in ('banner_passthrough.c', 'banner_passthrough.h'):
    shutil.copy(os.path.join(here, f), os.path.join(wrapper, f))

patch(os.path.join(wrapper, 'meson.build'), [(
    "  'wrapper_physical_device.c',\n)\n",
    "  'wrapper_physical_device.c',\n"
    "  'banner_passthrough.c',\n)\n", 1)])

# 1. The per-device flag.
patch(os.path.join(wrapper, 'wrapper_private.h'), [(
    "   struct wrapper_physical_device *physical;\n"
    "   struct vk_device_dispatch_table dispatch_table;\n"
    "};\n",
    "   struct wrapper_physical_device *physical;\n"
    "   struct vk_device_dispatch_table dispatch_table;\n"
    "   bool banner_passthrough; /* Bannerlator: VkCommandBuffer entry points are the driver's own (banner_passthrough.h) */\n"
    "};\n", 1)])

# 2. wrapper_device.c
patch(os.path.join(wrapper, 'wrapper_device.c'), [
    ('#include "wrapper_trampolines.h"\n#include "vk_alloc.h"\n',
     '#include "wrapper_trampolines.h"\n#include "banner_passthrough.h"\n#include "vk_alloc.h"\n', 1),
    # vkCreateDevice: decide + log, after the driver dispatch table and the queues exist.
    ("      device->vk.dispatch_table.FreeMemory =\n"
     "         wrapper_device_trampolines.FreeMemory;\n"
     "   }\n"
     "\n"
     "   *pDevice = wrapper_device_to_handle(device);\n",
     "      device->vk.dispatch_table.FreeMemory =\n"
     "         wrapper_device_trampolines.FreeMemory;\n"
     "   }\n"
     "\n"
     "   banner_pt_device_init(device); /* Bannerlator: pass-through dispatch for VkCommandBuffer entry points */\n"
     "\n"
     "   *pDevice = wrapper_device_to_handle(device);\n", 1),
    # vkGetDeviceProcAddr
    ("   VK_FROM_HANDLE(wrapper_device, device, _device);\n"
     "   return vk_device_get_proc_addr(&device->vk, pName);\n",
     "   VK_FROM_HANDLE(wrapper_device, device, _device);\n"
     "   return banner_pt_get_device_proc_addr(device, pName);\n", 1),
    # vkQueueSubmit / vkQueueSubmit2: the command buffers in the submits are already the driver's.
    ("   struct wrapper_fence *wf = get_wrapper_fence_from_handle(queue->device, fence);\n"
     "\n"
     "   for (int i = 0; i < submitCount; i++) {\n"
     "      const VkSubmitInfo *submit_info = &pSubmits[i];\n",
     "   if (queue->device->banner_passthrough)\n"
     "      return queue->device->dispatch_table.QueueSubmit(\n"
     "         queue->dispatch_handle, submitCount, pSubmits, fence);\n"
     "\n"
     "   struct wrapper_fence *wf = get_wrapper_fence_from_handle(queue->device, fence);\n"
     "\n"
     "   for (int i = 0; i < submitCount; i++) {\n"
     "      const VkSubmitInfo *submit_info = &pSubmits[i];\n", 1),
    ("   struct wrapper_fence *wf = get_wrapper_fence_from_handle(queue->device, fence);\n"
     "\n"
     "   for (int i = 0; i < submitCount; i++) {\n"
     "      const VkSubmitInfo2 *submit_info = &pSubmits[i];\n",
     "   if (queue->device->banner_passthrough)\n"
     "      return queue->device->dispatch_table.QueueSubmit2(\n"
     "         queue->dispatch_handle, submitCount, pSubmits, fence);\n"
     "\n"
     "   struct wrapper_fence *wf = get_wrapper_fence_from_handle(queue->device, fence);\n"
     "\n"
     "   for (int i = 0; i < submitCount; i++) {\n"
     "      const VkSubmitInfo2 *submit_info = &pSubmits[i];\n", 1),
    # vkAllocateCommandBuffers: hand out the driver's handles, remember their device.
    ("   result = device->dispatch_table.AllocateCommandBuffers(\n"
     "      device->dispatch_handle, pAllocateInfo, pCommandBuffers);\n"
     "   if (result != VK_SUCCESS)\n"
     "      return result;\n"
     "\n"
     "   simple_mtx_lock(&device->resource_mutex);\n"
     "\n"
     "   for (i = 0; i < pAllocateInfo->commandBufferCount; i++) {\n"
     "      result = wrapper_command_buffer_create(\n",
     "   result = device->dispatch_table.AllocateCommandBuffers(\n"
     "      device->dispatch_handle, pAllocateInfo, pCommandBuffers);\n"
     "   if (result != VK_SUCCESS)\n"
     "      return result;\n"
     "\n"
     "   if (device->banner_passthrough) {\n"
     "      banner_pt_track_cmds(device, pAllocateInfo->commandPool,\n"
     "                           pAllocateInfo->commandBufferCount, pCommandBuffers);\n"
     "      return VK_SUCCESS;\n"
     "   }\n"
     "\n"
     "   simple_mtx_lock(&device->resource_mutex);\n"
     "\n"
     "   for (i = 0; i < pAllocateInfo->commandBufferCount; i++) {\n"
     "      result = wrapper_command_buffer_create(\n", 1),
    # vkFreeCommandBuffers
    ("   VK_FROM_HANDLE(wrapper_device, device, _device);\n"
     "\n"
     "   simple_mtx_lock(&device->resource_mutex);\n"
     "\n"
     "   for (int i = 0; i < commandBufferCount; i++) {\n"
     "      VK_FROM_HANDLE(wrapper_command_buffer, wcb, pCommandBuffers[i]);\n"
     "      wrapper_command_buffer_destroy(device, wcb);\n"
     "   }\n",
     "   VK_FROM_HANDLE(wrapper_device, device, _device);\n"
     "\n"
     "   if (device->banner_passthrough) {\n"
     "      banner_pt_untrack_cmds(commandBufferCount, pCommandBuffers);\n"
     "      device->dispatch_table.FreeCommandBuffers(device->dispatch_handle, commandPool,\n"
     "                                                commandBufferCount, pCommandBuffers);\n"
     "      return;\n"
     "   }\n"
     "\n"
     "   simple_mtx_lock(&device->resource_mutex);\n"
     "\n"
     "   for (int i = 0; i < commandBufferCount; i++) {\n"
     "      VK_FROM_HANDLE(wrapper_command_buffer, wcb, pCommandBuffers[i]);\n"
     "      wrapper_command_buffer_destroy(device, wcb);\n"
     "   }\n", 1),
    # vkDestroyCommandPool: its command buffers go with it.
    ("   device->dispatch_table.DestroyCommandPool(device->dispatch_handle,\n"
     "                                             commandPool, pAllocator);\n",
     "   if (device->banner_passthrough)\n"
     "      banner_pt_untrack_pool(device, commandPool);\n"
     "\n"
     "   device->dispatch_table.DestroyCommandPool(device->dispatch_handle,\n"
     "                                             commandPool, pAllocator);\n", 1),
    # vkDestroyDevice
    ("   if (device->dispatch_handle != VK_NULL_HANDLE) {\n"
     "      device->dispatch_table.DestroyDevice(device->\n"
     "         dispatch_handle, pAllocator);\n"
     "   }\n",
     "   banner_pt_untrack_device(device);\n"
     "   if (device->dispatch_handle != VK_NULL_HANDLE) {\n"
     "      device->dispatch_table.DestroyDevice(device->\n"
     "         dispatch_handle, pAllocator);\n"
     "   }\n", 1),
    # vkSetPrivateData / vkGetPrivateData: a command buffer handle is already the driver's.
    ("static uint64_t\n"
     "unwrap_device_object(VkObjectType objectType,\n"
     "                     uint64_t objectHandle)\n"
     "{\n",
     "static uint64_t\n"
     "unwrap_device_object(struct wrapper_device *device,\n"
     "                     VkObjectType objectType,\n"
     "                     uint64_t objectHandle)\n"
     "{\n", 1),
    ("   case VK_OBJECT_TYPE_COMMAND_BUFFER:\n"
     "      return (uint64_t)(uintptr_t)wrapper_command_buffer_from_handle((VkCommandBuffer)(uintptr_t)objectHandle)->dispatch_handle;\n",
     "   case VK_OBJECT_TYPE_COMMAND_BUFFER:\n"
     "      if (device->banner_passthrough)\n"
     "         return objectHandle;\n"
     "      return (uint64_t)(uintptr_t)wrapper_command_buffer_from_handle((VkCommandBuffer)(uintptr_t)objectHandle)->dispatch_handle;\n", 1),
    ("   uint64_t object_handle = unwrap_device_object(objectType, objectHandle);\n",
     "   uint64_t object_handle = unwrap_device_object(device, objectType, objectHandle);\n", 2),
])

# 3. wrapper_instance.c: vkGetInstanceProcAddr for a VkCommandBuffer name -> side-table trampoline.
patch(os.path.join(wrapper, 'wrapper_instance.c'), [
    ('#include "graphicsenv_hook.hpp"\n',
     '#include "graphicsenv_hook.hpp"\n#include "banner_passthrough.h"\n', 1),
    ("   VK_FROM_HANDLE(wrapper_instance, instance, _instance);\n"
     "   return vk_instance_get_proc_addr(&instance->vk,\n"
     "                                    &wrapper_instance_entrypoints,\n"
     "                                    pName);\n",
     "   VK_FROM_HANDLE(wrapper_instance, instance, _instance);\n"
     "   return banner_pt_instance_proc_addr(\n"
     "      vk_instance_get_proc_addr(&instance->vk, &wrapper_instance_entrypoints, pName), pName);\n", 1),
])

# 4. wrapper_physical_device.c: the WSI's proc_addr (wsi_device_init) the same way.
patch(os.path.join(wrapper, 'wrapper_physical_device.c'), [
    ('#include "util/os_misc.h"\n',
     '#include "util/os_misc.h"\n#include "banner_passthrough.h"\n', 1),
    ("   VK_FROM_HANDLE(vk_physical_device, pdevice, physicalDevice);\n"
     "   return vk_instance_get_proc_addr_unchecked(pdevice->instance, pName);\n",
     "   VK_FROM_HANDLE(vk_physical_device, pdevice, physicalDevice);\n"
     "   return banner_pt_instance_proc_addr(\n"
     "      vk_instance_get_proc_addr_unchecked(pdevice->instance, pName), pName);\n", 1),
])

# 5. The trampolines generator: one side-table trampoline per VkCommandBuffer entry point.
CMD_TRAMPOLINES = r'''
/* Bannerlator pass-through (banner_passthrough.h): trampolines for callers that hold a command buffer
 * but do not know its device (the WSI, vkGetInstanceProcAddr). A pass-through device's command buffers
 * are the driver's, looked up in the side table; a fully wrapped device's are wrapper_command_buffer. */
% for e in entrypoints:
  % if not e.is_device_entrypoint() or e.alias or e.params[0].type != 'VkCommandBuffer':
    <% continue %>
  % endif
  % if e.guard is not None:
#ifdef ${e.guard}
  % endif
static VKAPI_ATTR ${e.return_type} VKAPI_CALL
${e.prefixed_name('banner_pt_tramp')}(${e.decl_params()})
{
    struct wrapper_device *banner_dev = banner_pt_device_for_cmd(${e.params[0].name});
    if (banner_dev) {
  % if e.return_type == 'void':
        banner_dev->dispatch_table.${e.name}(${e.call_params()});
        return;
  % else:
        return banner_dev->dispatch_table.${e.name}(${e.call_params()});
  % endif
    }
    VK_FROM_HANDLE(wrapper_command_buffer, wcb, ${e.params[0].name});
  % if e.return_type == 'void':
    wcb->device->dispatch_table.${e.name}(wcb->dispatch_handle${', ' + e.call_params(1) if len(e.params) > 1 else ''});
  % else:
    return wcb->device->dispatch_table.${e.name}(wcb->dispatch_handle${', ' + e.call_params(1) if len(e.params) > 1 else ''});
  % endif
}
  % if e.guard is not None:
#endif
  % endif
% endfor

struct vk_device_entrypoint_table banner_pt_command_buffer_trampolines = {
% for e in entrypoints:
  % if not e.is_device_entrypoint() or e.alias or e.params[0].type != 'VkCommandBuffer':
    <% continue %>
  % endif
  % if e.guard is not None:
#ifdef ${e.guard}
  % endif
    .${e.name} = ${e.prefixed_name('banner_pt_tramp')},
  % if e.guard is not None:
#endif
  % endif
% endfor
};

'''

patch(os.path.join(wrapper, 'vk_wrapper_trampolines_gen.py'), [
    ('#include "wrapper_private.h"\n#include "wrapper_trampolines.h"\n',
     '#include "wrapper_private.h"\n#include "wrapper_trampolines.h"\n#include "banner_passthrough.h"\n', 1),
    ('struct vk_device_entrypoint_table wrapper_device_trampolines = {\n',
     CMD_TRAMPOLINES + 'struct vk_device_entrypoint_table wrapper_device_trampolines = {\n', 1),
])

print("pipetto: pass-through dispatch applied (banner_passthrough.c, side-table command-buffer trampolines, "
      "BANNER_WRAPPER_PASSTHROUGH switch)")
