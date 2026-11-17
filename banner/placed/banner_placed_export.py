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

Wine's OpenGL device enables the VK_EXT_map_memory_placed extension but not the memoryMapPlaced feature (it only
queries it), so the wrapper's allocation emulation, which required the feature, never ran for it; the extension
alone now turns the emulation on, as drivers with native placed mapping behave.

vkFlushMappedMemoryRanges / vkInvalidateMappedMemoryRanges of memory the wrapper mapped itself went to the driver,
which cleans its own CPU mapping by address (Turnip: bo->map, NULL for such memory) and faulted; Wine flushes
every GL buffer write ("glFlushMappedBufferRange returned 0xc0000005", then its GL lock stays held and rendering
stops). The wrapper now cleans / invalidates its own mapping for those ranges (dc cvac / dc civac by cache line,
as Turnip does) and passes the rest to the driver.
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

patch(
    "   if (!device->vk.enabled_features.memoryMapPlaced ||\n"
    "       !device->vk.enabled_extensions.EXT_map_memory_placed)\n"
    "      goto fallback;\n",
    "   /* Wine's wow64 OpenGL enables VK_EXT_map_memory_placed without the memoryMapPlaced feature and maps\n"
    "    * placed anyway (drivers with native support honour that): the extension is enough here. */\n"
    "   if (!device->vk.enabled_extensions.EXT_map_memory_placed)\n"
    "      goto fallback;\n",
)

patch("#include <sys/mman.h>\n", "#include <sys/mman.h>\n#include <stdlib.h>\n")

patch(
    "VKAPI_ATTR void VKAPI_CALL\nwrapper_UnmapMemory(",
    "/* Flush / invalidate of memory the wrapper mapped itself: the driver keeps no CPU mapping of it (Turnip cleans\n"
    " * its own bo->map line by line and would touch NULL), so the wrapper cleans or invalidates its own mapping\n"
    " * the same way; ranges of other memory go to the driver unchanged (banner/placed/banner_placed_export.py). */\n"
    "static void\n"
    "banner_sync_cache_range(const struct wrapper_device_memory *mem, VkDeviceSize offset, VkDeviceSize size,\n"
    "                        bool to_gpu)\n"
    "{\n"
    "   uint64_t ctr;\n"
    "   uintptr_t line, start, end;\n"
    "\n"
    "   if (!mem->map_address || offset >= mem->map_size)\n"
    "      return;\n"
    "   __asm__ volatile(\"mrs %0, ctr_el0\" : \"=r\"(ctr));\n"
    "   line = (uintptr_t)4 << ((ctr >> 16) & 0xf);\n"
    "   start = (uintptr_t)mem->map_address + offset;\n"
    "   end = (size == VK_WHOLE_SIZE || offset + size > mem->map_size) ?\n"
    "      (uintptr_t)mem->map_address + mem->map_size : start + size;\n"
    "   for (start &= ~(line - 1); start < end; start += line) {\n"
    "      if (to_gpu)\n"
    "         __asm__ volatile(\"dc cvac, %0\" :: \"r\"(start) : \"memory\");\n"
    "      else\n"
    "         __asm__ volatile(\"dc civac, %0\" :: \"r\"(start) : \"memory\");\n"
    "   }\n"
    "   __asm__ volatile(\"dsb sy\" ::: \"memory\");\n"
    "}\n"
    "\n"
    "static VkResult\n"
    "banner_sync_ranges(VkDevice _device, uint32_t count, const VkMappedMemoryRange *ranges, bool to_gpu)\n"
    "{\n"
    "   VK_FROM_HANDLE(wrapper_device, device, _device);\n"
    "   VkMappedMemoryRange stack_ranges[16];\n"
    "   VkMappedMemoryRange *rest = count <= 16 ? stack_ranges : malloc(count * sizeof(*rest));\n"
    "   VkResult result = VK_SUCCESS;\n"
    "   uint32_t n = 0;\n"
    "\n"
    "   if (!rest)\n"
    "      return VK_ERROR_OUT_OF_HOST_MEMORY;\n"
    "   for (uint32_t i = 0; i < count; i++) {\n"
    "      struct wrapper_device_memory *mem =\n"
    "         wrapper_device_memory_from_handle(device, ranges[i].memory);\n"
    "      if (mem && mem->map_address)\n"
    "         banner_sync_cache_range(mem, ranges[i].offset, ranges[i].size, to_gpu);\n"
    "      else\n"
    "         rest[n++] = ranges[i];\n"
    "   }\n"
    "   if (n)\n"
    "      result = to_gpu ?\n"
    "         device->dispatch_table.FlushMappedMemoryRanges(device->dispatch_handle, n, rest) :\n"
    "         device->dispatch_table.InvalidateMappedMemoryRanges(device->dispatch_handle, n, rest);\n"
    "   if (rest != stack_ranges)\n"
    "      free(rest);\n"
    "   return result;\n"
    "}\n"
    "\n"
    "VKAPI_ATTR VkResult VKAPI_CALL\n"
    "wrapper_FlushMappedMemoryRanges(VkDevice device, uint32_t memoryRangeCount,\n"
    "                                const VkMappedMemoryRange *pMemoryRanges)\n"
    "{\n"
    "   return banner_sync_ranges(device, memoryRangeCount, pMemoryRanges, true);\n"
    "}\n"
    "\n"
    "VKAPI_ATTR VkResult VKAPI_CALL\n"
    "wrapper_InvalidateMappedMemoryRanges(VkDevice device, uint32_t memoryRangeCount,\n"
    "                                     const VkMappedMemoryRange *pMemoryRanges)\n"
    "{\n"
    "   return banner_sync_ranges(device, memoryRangeCount, pMemoryRanges, false);\n"
    "}\n"
    "\n"
    "VKAPI_ATTR void VKAPI_CALL\nwrapper_UnmapMemory(",
)

print("pipetto: placed mapping for exportable host-visible memory applied")
