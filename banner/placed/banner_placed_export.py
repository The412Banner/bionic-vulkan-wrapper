#!/usr/bin/env python3
"""Placed mapping for exportable host-visible memory (Pipetto wrapper-25 tree).

The wrapper emulates VK_EXT_map_memory_placed for drivers without it (Turnip on KGSL): wrapper_AllocateMemory
allocates host-visible memory from its own dma-buf / AHardwareBuffer / opaque-fd backend, keeps the fd, and
wrapper_MapMemory2KHR maps that fd MAP_FIXED at the program's pPlacedAddress. Allocations that ask for export
(VkExportMemoryAllocateInfo) skipped the emulation and went straight to the driver, with no fd kept; a placed
map of such memory then fell back to the driver's plain vkMapMemory and returned VK_SUCCESS at an address the
program did not ask for.

Wine's wow64 OpenGL does exactly that: for a 32-bit program it backs GL buffers with exportable Vulkan memory
(imported into GL through GL_EXT_memory_object_fd) and maps it placed below 4 GB; the wrong address trips
"wow64_map_buffer: assertion buffer->host_ptr == buffer->vm_ptr failed" and the program dies (32-bit WineD3D:
DirectDraw / Direct3D 1-7, and 32-bit OpenGL games).

Here exportable host-visible memory is still allocated by the driver exactly as asked, then exported once
more (vkGetMemoryFdKHR, dma-buf first, else opaque fd) so the wrapper keeps an fd and records the allocation
like its own; the existing placed map, unmap and free code then handles it. If the driver gives no fd, the
allocation stays plain. A placed map of memory the wrapper does not track now fails with
VK_ERROR_MEMORY_MAP_FAILED instead of returning another address.
"""
import os
import sys

root = sys.argv[1]
rel = "src/vulkan/wrapper/wrapper_device_memory.c"


def patch(old, new):
    p = os.path.join(root, rel)
    s = open(p).read()
    assert s.count(old) == 1, (rel, old[:70])
    open(p, "w").write(s.replace(old, new))


patch(
    "VKAPI_ATTR VkResult VKAPI_CALL\nwrapper_AllocateMemory(",
    "/* Exportable host-visible memory: allocated by the driver as asked, then exported once more so the wrapper\n"
    " * keeps an fd and can place its mappings (banner/placed/banner_placed_export.py). */\n"
    "static VkResult\n"
    "banner_allocate_exportable(struct wrapper_device *device,\n"
    "                           const VkMemoryAllocateInfo *pAllocateInfo,\n"
    "                           const VkAllocationCallbacks *pAllocator,\n"
    "                           VkDeviceMemory *pMemory,\n"
    "                           const VkExportMemoryAllocateInfo *export_info)\n"
    "{\n"
    "   struct wrapper_device_memory *mem;\n"
    "   VkExternalMemoryHandleTypeFlagBits type;\n"
    "   VkResult result;\n"
    "   int fd = -1;\n"
    "\n"
    "   result = device->dispatch_table.AllocateMemory(device->dispatch_handle,\n"
    "      pAllocateInfo, pAllocator, pMemory);\n"
    "   if (result != VK_SUCCESS)\n"
    "      return result;\n"
    "\n"
    "   if (export_info->handleTypes & VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT)\n"
    "      type = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;\n"
    "   else if (export_info->handleTypes & VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT)\n"
    "      type = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;\n"
    "   else\n"
    "      return VK_SUCCESS;\n"
    "\n"
    "   VkMemoryGetFdInfoKHR get_fd = {\n"
    "      .sType = VK_STRUCTURE_TYPE_MEMORY_GET_FD_INFO_KHR,\n"
    "      .memory = *pMemory,\n"
    "      .handleType = type,\n"
    "   };\n"
    "   if (device->dispatch_table.GetMemoryFdKHR(device->dispatch_handle, &get_fd, &fd) != VK_SUCCESS ||\n"
    "       fd < 0) {\n"
    "      WRAPPER_LOG(info, \"exportable memory without an fd: placed mapping not available for it\");\n"
    "      return VK_SUCCESS;\n"
    "   }\n"
    "\n"
    "   simple_mtx_lock(&device->resource_mutex);\n"
    "   if (wrapper_device_memory_create(device, pAllocator, &mem) != VK_SUCCESS) {\n"
    "      simple_mtx_unlock(&device->resource_mutex);\n"
    "      close(fd);\n"
    "      return VK_SUCCESS;\n"
    "   }\n"
    "   mem->fd = fd;\n"
    "   mem->alloc_size = pAllocateInfo->allocationSize;\n"
    "   mem->dispatch_handle = *pMemory;\n"
    "   simple_mtx_unlock(&device->resource_mutex);\n"
    "   WRAPPER_LOG(info, \"exportable memory kept for placed mapping, size %llu\",\n"
    "               (unsigned long long)pAllocateInfo->allocationSize);\n"
    "   return VK_SUCCESS;\n"
    "}\n"
    "\n"
    "VKAPI_ATTR VkResult VKAPI_CALL\nwrapper_AllocateMemory(",
)

patch(
    "   if (vk_find_struct_const(pAllocateInfo, EXPORT_MEMORY_ALLOCATE_INFO))\n"
    "      goto fallback;\n",
    "   {\n"
    "      const VkExportMemoryAllocateInfo *banner_export =\n"
    "         vk_find_struct_const(pAllocateInfo, EXPORT_MEMORY_ALLOCATE_INFO);\n"
    "      if (banner_export)\n"
    "         return banner_allocate_exportable(device, pAllocateInfo, pAllocator, pMemory,\n"
    "                                           banner_export);\n"
    "   }\n",
)

patch(
    "   mem = wrapper_device_memory_from_handle(device, pMemoryMapInfo->memory);\n"
    "   if (!placed_info || !mem) {\n",
    "   mem = wrapper_device_memory_from_handle(device, pMemoryMapInfo->memory);\n"
    "   if (placed_info && !mem) {\n"
    "      WRAPPER_LOG(error, \"placed map of memory the wrapper does not track: cannot place it\");\n"
    "      return VK_ERROR_MEMORY_MAP_FAILED;\n"
    "   }\n"
    "   if (!placed_info || !mem) {\n",
)

print("pipetto: placed mapping for exportable host-visible memory applied")
