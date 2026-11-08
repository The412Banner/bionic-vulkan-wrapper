#!/usr/bin/env python3
# Bannerlator: BCn texture emulation fixes (Pipetto wrapper-25 tree).
#
# A driver without textureCompressionBC (the Arm Mali driver) gets BC1-7 textures from the wrapper as
# plain RGBA8 / R8 / RG8 / RGB16F images, and the game's compressed data is unpacked on the CPU in
# vkCmdCopyBufferToImage. Gaps fixed here:
#  1. vkCmdCopyBufferToImage2, the call DXVK 2+ and vkd3d-proton record, went straight to the driver:
#     the compressed bytes landed in the unpacked image as they were (striped, garbled textures).
#  2. vkGetDeviceImageMemoryRequirements (vkd3d-proton's GetResourceAllocationInfo) answered for the
#     compressed format while the image the wrapper creates is the unpacked one (4x for BC7 -> RGBA8):
#     placed textures did not fit their heaps ("Heap too small for the texture") and the game failed.
#  3. The game's upload buffer memory was mapped again by the wrapper and unmapped after, while the game
#     keeps it mapped (invalid; the copy was skipped when the driver refused). The game's own mapping is
#     now used when it has one; only memory nobody has mapped is mapped (and unmapped) here.
#  4. Staging buffers were freed only through the fence of an earlier submission (fences are attached at
#     submit, the copy is recorded before), i.e. never for DXVK / vkd3d-proton. They now belong to the
#     command buffer and are freed when it is begun again, reset or freed, or its pool is reset/destroyed.
# Also: source rows padded with bufferRowLength / bufferImageHeight, extents that are not a multiple of
# four (small mips: the old code wrote past its staging buffer) and several layers or depth slices in one
# region are unpacked correctly; the staging memory is host-coherent (or flushed).
#  5. vkBindBufferMemory2 (vkd3d-proton's bind) was not seen, so the wrapper did not know which memory a
#     D3D12 upload buffer lives in and could not read it.
#  6. The Arm driver can have BCn of its own (a BCn shim in a custom build, e.g. kirimu's): then the
#     default ("auto", 3) leaves BCn to the driver, as it already does for Turnip and the newer Qualcomm
#     driver. WRAPPER_EMULATE_BCN=2 still forces emulation, =0 forces it off.
# Installed only where the wrapper emulates BCn (emulate_bcn >= 2: its default for every driver but Turnip
# and the newer Qualcomm driver, i.e. the Arm Mali driver and PanVK through the adapter, unless
# WRAPPER_EMULATE_BCN says otherwise). On Turnip (Adreno) nothing is installed and nothing changes.
import os
import sys

root = sys.argv[1]


def patch(rel, old, new):
    p = os.path.join(root, rel)
    s = open(p).read()
    assert s.count(old) == 1, (rel, old[:70], s.count(old))
    open(p, "w").write(s.replace(old, new))


def replace_between(rel, start, end, new):
    """Replace s[start:end) (start included, end kept) with new; both markers must be unique."""
    p = os.path.join(root, rel)
    s = open(p).read()
    assert s.count(start) == 1 and s.count(end) == 1, (rel, start[:60], s.count(start), end[:60], s.count(end))
    a = s.index(start)
    b = s.index(end)
    assert a < b, (rel, "markers out of order")
    open(p, "w").write(s[:a] + new + s[b:])


PRIV = "src/vulkan/wrapper/wrapper_private.h"
DEV = "src/vulkan/wrapper/wrapper_device.c"

# Per-device state and the per-command-buffer staging list.
patch(PRIV,
      "   struct wrapper_physical_device *physical;\n   struct vk_device_dispatch_table dispatch_table;\n",
      "   struct wrapper_physical_device *physical;\n   struct vk_device_dispatch_table dispatch_table;\n"
      "   struct banner_bcn_state *banner_bcn; /* banner/mali/banner_mali_bcn.py */\n")
patch(PRIV,
      "   struct wrapper_fence *fence;\n   VkCommandBuffer dispatch_handle;\n};",
      "   struct wrapper_fence *fence;\n   VkCommandBuffer dispatch_handle;\n"
      "   struct list_head banner_staging; /* banner/mali/banner_mali_bcn.py; zeroed = empty */\n};")

# Forward declarations (the code itself is appended at the end of the file).
patch(DEV, '#include "util/simple_mtx.h"\n',
      '#include "util/simple_mtx.h"\n'
      '#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n'
      '\n/* Bannerlator: banner/mali/banner_mali_bcn.py */\n'
      'struct wrapper_command_buffer;\nstruct wrapper_image;\nstruct wrapper_buffer;\n'
      'static void banner_bcn_install(struct wrapper_device *device);\n'
      'static void banner_bcn_upload(struct wrapper_command_buffer *wcb, struct wrapper_image *wi,\n'
      '                              struct wrapper_buffer *wb, VkImage dstImage, VkImageLayout dstLayout,\n'
      '                              uint32_t regionCount, const VkBufferImageCopy *pRegions);\n')

# Hooks go in once the device's dispatch table is final.
patch(DEV, "   *pDevice = wrapper_device_to_handle(device);\n",
      "   banner_bcn_install(device);\n\n   *pDevice = wrapper_device_to_handle(device);\n")

# vkCmdCopyBufferToImage: the emulated branch goes through the shared upload below.
replace_between(DEV,
                "wrapper_CmdCopyBufferToImage(VkCommandBuffer commandBuffer,",
                "VKAPI_ATTR void VKAPI_CALL\nwrapper_FreeCommandBuffers(",
                r'''wrapper_CmdCopyBufferToImage(VkCommandBuffer commandBuffer,
                             VkBuffer srcBuffer,
                             VkImage dstImage,
                             VkImageLayout dstLayout,
                             uint32_t regionCount,
                             const VkBufferImageCopy *pRegions)
{
   VK_FROM_HANDLE(wrapper_command_buffer, wcb, commandBuffer);
   struct wrapper_device *device = wcb->device;
   struct wrapper_image *wi = get_wrapper_image_from_handle(device, dstImage);
   struct wrapper_buffer *wb = get_wrapper_buffer_from_handle(device, srcBuffer);

   if (!wi || !wb || !is_emulated_bcn(device->physical, wi->info.format)) {
      device->dispatch_table.CmdCopyBufferToImage(wcb->dispatch_handle,
         srcBuffer, dstImage, dstLayout, regionCount, pRegions);
      return;
   }

   banner_bcn_upload(wcb, wi, wb, dstImage, dstLayout, regionCount, pRegions);
}

''')

CODE = r'''

/* ---- Bannerlator: BCn emulation fixes (banner/mali/banner_mali_bcn.py) ----
 * Installed only when the wrapper emulates BCn (emulate_bcn >= 2). See the patch header for the why. */

#define BANNER_PREV(name) __typeof__(((struct vk_device_dispatch_table *)0)->name) name

struct banner_bcn_map {
   char *base;                 /* the game's pointer for memory offset 0 */
   VkDeviceSize offset, size;  /* the mapped range (size may be VK_WHOLE_SIZE) */
};

struct banner_bcn_state {
   simple_mtx_t mutex;
   struct hash_table_u64 *maps; /* VkDeviceMemory -> struct banner_bcn_map (game mappings) */
   BANNER_PREV(MapMemory);
   BANNER_PREV(MapMemory2);
   BANNER_PREV(UnmapMemory);
   BANNER_PREV(UnmapMemory2);
   BANNER_PREV(FreeMemory);
   BANNER_PREV(BeginCommandBuffer);
   BANNER_PREV(ResetCommandBuffer);
   BANNER_PREV(ResetCommandPool);
   BANNER_PREV(FreeCommandBuffers);
   BANNER_PREV(DestroyCommandPool);
   BANNER_PREV(CmdCopyBufferToImage2);
   BANNER_PREV(GetDeviceImageMemoryRequirements);
   BANNER_PREV(BindBufferMemory2);
};

static void
banner_bcn_map_set(struct banner_bcn_state *st, VkDeviceMemory memory, char *base,
                   VkDeviceSize offset, VkDeviceSize size)
{
   struct banner_bcn_map *m;

   simple_mtx_lock(&st->mutex);
   m = _mesa_hash_table_u64_search(st->maps, (uint64_t)memory);
   if (!m) {
      m = calloc(1, sizeof(*m));
      if (m)
         _mesa_hash_table_u64_insert(st->maps, (uint64_t)memory, m);
   }
   if (m) {
      m->base = base;
      m->offset = offset;
      m->size = size;
   }
   simple_mtx_unlock(&st->mutex);
}

static void
banner_bcn_map_drop(struct banner_bcn_state *st, VkDeviceMemory memory)
{
   struct banner_bcn_map *m;

   if (memory == VK_NULL_HANDLE)
      return;
   simple_mtx_lock(&st->mutex);
   m = _mesa_hash_table_u64_search(st->maps, (uint64_t)memory);
   if (m) {
      _mesa_hash_table_u64_remove(st->maps, (uint64_t)memory);
      free(m);
   }
   simple_mtx_unlock(&st->mutex);
}

static VKAPI_ATTR VkResult VKAPI_CALL
banner_MapMemory(VkDevice _device, VkDeviceMemory memory, VkDeviceSize offset,
                 VkDeviceSize size, VkMemoryMapFlags flags, void **ppData)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);
   struct banner_bcn_state *st = device->banner_bcn;
   VkResult res = st->MapMemory(_device, memory, offset, size, flags, ppData);

   if (res == VK_SUCCESS && ppData && *ppData)
      banner_bcn_map_set(st, memory, (char *)*ppData - offset, offset, size);
   return res;
}

static VKAPI_ATTR VkResult VKAPI_CALL
banner_MapMemory2(VkDevice _device, const VkMemoryMapInfoKHR *pMemoryMapInfo, void **ppData)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);
   struct banner_bcn_state *st = device->banner_bcn;
   VkResult res = st->MapMemory2(_device, pMemoryMapInfo, ppData);

   if (res == VK_SUCCESS && pMemoryMapInfo && ppData && *ppData)
      banner_bcn_map_set(st, pMemoryMapInfo->memory, (char *)*ppData - pMemoryMapInfo->offset,
                         pMemoryMapInfo->offset, pMemoryMapInfo->size);
   return res;
}

static VKAPI_ATTR void VKAPI_CALL
banner_UnmapMemory(VkDevice _device, VkDeviceMemory memory)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);
   struct banner_bcn_state *st = device->banner_bcn;

   banner_bcn_map_drop(st, memory);
   st->UnmapMemory(_device, memory);
}

static VKAPI_ATTR VkResult VKAPI_CALL
banner_UnmapMemory2(VkDevice _device, const VkMemoryUnmapInfoKHR *pMemoryUnmapInfo)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);
   struct banner_bcn_state *st = device->banner_bcn;

   if (pMemoryUnmapInfo)
      banner_bcn_map_drop(st, pMemoryUnmapInfo->memory);
   return st->UnmapMemory2(_device, pMemoryUnmapInfo);
}

static VKAPI_ATTR void VKAPI_CALL
banner_FreeMemory(VkDevice _device, VkDeviceMemory memory, const VkAllocationCallbacks *pAllocator)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);
   struct banner_bcn_state *st = device->banner_bcn;

   banner_bcn_map_drop(st, memory);
   st->FreeMemory(_device, memory, pAllocator);
}

/* vkd3d-proton binds with vkBindBufferMemory2: record the memory like wrapper_BindBufferMemory does. */
static VKAPI_ATTR VkResult VKAPI_CALL
banner_BindBufferMemory2(VkDevice _device, uint32_t bindInfoCount, const VkBindBufferMemoryInfo *pBindInfos)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);
   VkResult res = device->banner_bcn->BindBufferMemory2(_device, bindInfoCount, pBindInfos);

   if (res != VK_SUCCESS)
      return res;
   for (uint32_t i = 0; i < bindInfoCount; i++) {
      struct wrapper_buffer *wb = get_wrapper_buffer_from_handle(device, pBindInfos[i].buffer);
      if (wb) {
         wb->memory = pBindInfos[i].memory;
         wb->offset = pBindInfos[i].memoryOffset;
      }
   }
   return res;
}

/* Staging buffers made for one command buffer's uploads: freed once it can no longer run them. */
static void
banner_bcn_free_staging(struct wrapper_device *device, struct list_head *list)
{
   list_for_each_entry_safe(struct wrapper_buffer, staging, list, link) {
      VkDeviceMemory memory = staging->memory;
      wrapper_buffer_destroy(device, staging, NULL);
      device->dispatch_table.FreeMemory(device->dispatch_handle, memory, NULL);
   }
}

static void
banner_bcn_release_cb(struct wrapper_command_buffer *wcb)
{
   if (!wcb)
      return;
   if (!wcb->banner_staging.next) {
      list_inithead(&wcb->banner_staging);
      return;
   }
   banner_bcn_free_staging(wcb->device, &wcb->banner_staging);
   list_inithead(&wcb->banner_staging);
}

static void
banner_bcn_release_pool(struct wrapper_device *device, VkCommandPool pool)
{
   struct list_head gone;

   list_inithead(&gone);
   simple_mtx_lock(&device->resource_mutex);
   list_for_each_entry(struct wrapper_command_buffer, wcb, &device->command_buffer_list, link) {
      if (wcb->pool != pool || !wcb->banner_staging.next)
         continue;
      list_splicetail(&wcb->banner_staging, &gone);
      list_inithead(&wcb->banner_staging);
   }
   simple_mtx_unlock(&device->resource_mutex);
   banner_bcn_free_staging(device, &gone);
}

static VKAPI_ATTR VkResult VKAPI_CALL
banner_BeginCommandBuffer(VkCommandBuffer commandBuffer, const VkCommandBufferBeginInfo *pBeginInfo)
{
   VK_FROM_HANDLE(wrapper_command_buffer, wcb, commandBuffer);

   banner_bcn_release_cb(wcb);
   return wcb->device->banner_bcn->BeginCommandBuffer(commandBuffer, pBeginInfo);
}

static VKAPI_ATTR VkResult VKAPI_CALL
banner_ResetCommandBuffer(VkCommandBuffer commandBuffer, VkCommandBufferResetFlags flags)
{
   VK_FROM_HANDLE(wrapper_command_buffer, wcb, commandBuffer);

   banner_bcn_release_cb(wcb);
   return wcb->device->banner_bcn->ResetCommandBuffer(commandBuffer, flags);
}

static VKAPI_ATTR VkResult VKAPI_CALL
banner_ResetCommandPool(VkDevice _device, VkCommandPool commandPool, VkCommandPoolResetFlags flags)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);

   banner_bcn_release_pool(device, commandPool);
   return device->banner_bcn->ResetCommandPool(_device, commandPool, flags);
}

static VKAPI_ATTR void VKAPI_CALL
banner_FreeCommandBuffers(VkDevice _device, VkCommandPool commandPool, uint32_t commandBufferCount,
                          const VkCommandBuffer *pCommandBuffers)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);

   for (uint32_t i = 0; i < commandBufferCount; i++) {
      if (pCommandBuffers[i] == VK_NULL_HANDLE)
         continue;
      VK_FROM_HANDLE(wrapper_command_buffer, wcb, pCommandBuffers[i]);
      banner_bcn_release_cb(wcb);
   }
   device->banner_bcn->FreeCommandBuffers(_device, commandPool, commandBufferCount, pCommandBuffers);
}

static VKAPI_ATTR void VKAPI_CALL
banner_DestroyCommandPool(VkDevice _device, VkCommandPool commandPool,
                          const VkAllocationCallbacks *pAllocator)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);

   if (commandPool != VK_NULL_HANDLE)
      banner_bcn_release_pool(device, commandPool);
   device->banner_bcn->DestroyCommandPool(_device, commandPool, pAllocator);
}

static int
banner_bcn_block_bytes(VkFormat format)
{
   switch (format) {
   case VK_FORMAT_BC1_RGB_UNORM_BLOCK:
   case VK_FORMAT_BC1_RGB_SRGB_BLOCK:
   case VK_FORMAT_BC1_RGBA_UNORM_BLOCK:
   case VK_FORMAT_BC1_RGBA_SRGB_BLOCK:
   case VK_FORMAT_BC4_UNORM_BLOCK:
   case VK_FORMAT_BC4_SNORM_BLOCK:
      return 8;
   default:
      return 16;
   }
}

/* One region's slices (layers x depth) unpacked into dst: each slice pw x ph texels (the extent rounded
 * up to whole 4x4 blocks), slices back to back. src points at the region's first block; its rows and
 * slices follow bufferRowLength / bufferImageHeight. */
static void
banner_bcn_unpack_region(const char *src, char *dst, const VkBufferImageCopy *r, uint32_t slices,
                         VkFormat format)
{
   const int block = banner_bcn_block_bytes(format);
   const int texel = get_texel_size_for_format(get_format_for_bcn(format));
   const uint32_t bx = (r->imageExtent.width + 3) / 4, by = (r->imageExtent.height + 3) / 4;
   const uint32_t row_texels = r->bufferRowLength ? r->bufferRowLength : r->imageExtent.width;
   const uint32_t slice_rows = r->bufferImageHeight ? r->bufferImageHeight : r->imageExtent.height;
   const size_t src_row = (size_t)((row_texels + 3) / 4) * block;
   const size_t src_slice = (size_t)((slice_rows + 3) / 4) * src_row;
   const size_t tight_row = (size_t)bx * block;
   const size_t dst_slice = (size_t)bx * 4 * by * 4 * texel;
   char *tight = NULL;

   if (src_row != tight_row)
      tight = malloc(tight_row * by);

   for (uint32_t s = 0; s < slices; s++) {
      const char *slice = src + s * src_slice;
      if (src_row != tight_row) {
         if (!tight)
            return;
         for (uint32_t y = 0; y < by; y++)
            memcpy(tight + y * tight_row, slice + y * src_row, tight_row);
         slice = tight;
      }
      decompress_bcn_format((void *)slice, dst + s * dst_slice, bx * 4, by * 4, format, 0);
   }
   free(tight);
}

/* The game's bytes for [offset, offset + size) of buffer wb: through the game's own mapping when it has
 * one, else through a mapping made here (*own, unmapped by the caller). */
static const char *
banner_bcn_source(struct wrapper_device *device, struct wrapper_buffer *wb, VkDeviceSize offset,
                  VkDeviceSize size, void **own)
{
   struct banner_bcn_state *st = device->banner_bcn;
   const VkDeviceSize start = wb->offset + offset;
   const char *p = NULL;

   if (st) {
      simple_mtx_lock(&st->mutex);
      struct banner_bcn_map *m = _mesa_hash_table_u64_search(st->maps, (uint64_t)wb->memory);
      if (m && start >= m->offset &&
          (m->size == VK_WHOLE_SIZE || start + size <= m->offset + m->size))
         p = m->base + start;
      simple_mtx_unlock(&st->mutex);
   }
   if (p)
      return p;
   if (!*own &&
       device->dispatch_table.MapMemory(device->dispatch_handle, wb->memory, 0, VK_WHOLE_SIZE, 0,
                                        own) != VK_SUCCESS) {
      *own = NULL;
      return NULL;
   }
   return *own ? (const char *)*own + start : NULL;
}

static uint32_t
banner_bcn_staging_type(struct wrapper_device *device, uint32_t type_bits, bool *coherent)
{
   const VkPhysicalDeviceMemoryProperties *props = &device->physical->memory_properties;
   const VkMemoryPropertyFlags want = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;

   for (uint32_t i = 0; i < props->memoryTypeCount; i++)
      if ((type_bits & (1u << i)) && (props->memoryTypes[i].propertyFlags & want) == want) {
         *coherent = true;
         return i;
      }
   for (uint32_t i = 0; i < props->memoryTypeCount; i++)
      if ((type_bits & (1u << i)) &&
          (props->memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)) {
         *coherent = false;
         return i;
      }
   return UINT32_MAX;
}

static void
banner_bcn_upload(struct wrapper_command_buffer *wcb, struct wrapper_image *wi, struct wrapper_buffer *wb,
                  VkImage dstImage, VkImageLayout dstLayout, uint32_t regionCount,
                  const VkBufferImageCopy *pRegions)
{
   struct wrapper_device *device = wcb->device;
   const VkFormat format = wi->info.format;
   const int block = banner_bcn_block_bytes(format);
   const int texel = get_texel_size_for_format(get_format_for_bcn(format));
   void *own = NULL;
   static bool said;

   if (!said) {
      said = true;
      fprintf(stderr, "wrapper-bcn: BCn textures unpacked on the CPU for this driver (first upload: %ux%u, "
              "format %d -> %d; vkCmdCopyBufferToImage2 + memory requirements + staging per command buffer)\n",
              pRegions && regionCount ? pRegions[0].imageExtent.width : 0,
              pRegions && regionCount ? pRegions[0].imageExtent.height : 0,
              (int)format, (int)get_format_for_bcn(format));
   }

   if (!wcb->banner_staging.next)
      list_inithead(&wcb->banner_staging);

   for (uint32_t i = 0; i < regionCount; i++) {
      VkBufferImageCopy region = pRegions[i];
      uint32_t layers = region.imageSubresource.layerCount;
      if (layers == VK_REMAINING_ARRAY_LAYERS)
         layers = wi->info.arrayLayers > region.imageSubresource.baseArrayLayer ?
                  wi->info.arrayLayers - region.imageSubresource.baseArrayLayer : 1;
      const uint32_t depth = region.imageExtent.depth ? region.imageExtent.depth : 1;
      const uint32_t slices = layers * depth;
      const uint32_t bx = (region.imageExtent.width + 3) / 4, by = (region.imageExtent.height + 3) / 4;
      if (!bx || !by || !slices)
         continue;

      const uint32_t row_texels = region.bufferRowLength ? region.bufferRowLength : region.imageExtent.width;
      const uint32_t slice_rows = region.bufferImageHeight ? region.bufferImageHeight : region.imageExtent.height;
      const VkDeviceSize src_row = (VkDeviceSize)((row_texels + 3) / 4) * block;
      const VkDeviceSize src_slice = (VkDeviceSize)((slice_rows + 3) / 4) * src_row;
      const VkDeviceSize src_size = src_slice * (slices - 1) + src_row * (by - 1) + (VkDeviceSize)bx * block;
      const VkDeviceSize dst_size = (VkDeviceSize)bx * 4 * by * 4 * texel * slices;

      const char *src = banner_bcn_source(device, wb, region.bufferOffset, src_size, &own);
      if (!src) {
         WRAPPER_LOG(error, "BCn upload: the source buffer could not be read");
         continue;
      }

      struct wrapper_buffer *staging = vk_object_zalloc(&device->vk, &device->vk.alloc,
         sizeof(struct wrapper_buffer), VK_OBJECT_TYPE_BUFFER);
      if (!staging)
         continue;

      VkBufferCreateInfo buffer_info = {
         .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
         .size = dst_size,
         .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
         .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
      };
      if (device->dispatch_table.CreateBuffer(device->dispatch_handle, &buffer_info, NULL,
                                              &staging->dispatch_handle) != VK_SUCCESS) {
         vk_object_free(&device->vk, &device->vk.alloc, staging);
         continue;
      }

      VkMemoryRequirements reqs;
      bool coherent = false;
      device->dispatch_table.GetBufferMemoryRequirements(device->dispatch_handle,
                                                         staging->dispatch_handle, &reqs);
      VkMemoryAllocateInfo alloc_info = {
         .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
         .allocationSize = reqs.size,
         .memoryTypeIndex = banner_bcn_staging_type(device, reqs.memoryTypeBits, &coherent),
      };
      void *mapped = NULL;
      if (alloc_info.memoryTypeIndex == UINT32_MAX ||
          device->dispatch_table.AllocateMemory(device->dispatch_handle, &alloc_info, NULL,
                                                &staging->memory) != VK_SUCCESS) {
         device->dispatch_table.DestroyBuffer(device->dispatch_handle, staging->dispatch_handle, NULL);
         vk_object_free(&device->vk, &device->vk.alloc, staging);
         continue;
      }
      if (device->dispatch_table.BindBufferMemory(device->dispatch_handle, staging->dispatch_handle,
                                                  staging->memory, 0) != VK_SUCCESS ||
          device->dispatch_table.MapMemory(device->dispatch_handle, staging->memory, 0, VK_WHOLE_SIZE, 0,
                                           &mapped) != VK_SUCCESS) {
         device->dispatch_table.DestroyBuffer(device->dispatch_handle, staging->dispatch_handle, NULL);
         device->dispatch_table.FreeMemory(device->dispatch_handle, staging->memory, NULL);
         vk_object_free(&device->vk, &device->vk.alloc, staging);
         continue;
      }

      banner_bcn_unpack_region(src, mapped, &region, slices, format);

      if (!coherent) {
         VkMappedMemoryRange range = {
            .sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
            .memory = staging->memory,
            .offset = 0,
            .size = VK_WHOLE_SIZE,
         };
         device->dispatch_table.FlushMappedMemoryRanges(device->dispatch_handle, 1, &range);
      }

      region.bufferOffset = 0;
      region.bufferRowLength = bx * 4;
      region.bufferImageHeight = by * 4;
      device->dispatch_table.CmdCopyBufferToImage(wcb->dispatch_handle, staging->dispatch_handle,
                                                  dstImage, dstLayout, 1, &region);

      staging->device = device;
      staging->wcb = wcb;
      list_addtail(&staging->link, &wcb->banner_staging);
   }

   if (own)
      device->dispatch_table.UnmapMemory(device->dispatch_handle, wb->memory);
}

static VKAPI_ATTR void VKAPI_CALL
banner_CmdCopyBufferToImage2(VkCommandBuffer commandBuffer, const VkCopyBufferToImageInfo2 *pInfo)
{
   VK_FROM_HANDLE(wrapper_command_buffer, wcb, commandBuffer);
   struct wrapper_device *device = wcb->device;
   struct wrapper_image *wi = pInfo ? get_wrapper_image_from_handle(device, pInfo->dstImage) : NULL;
   struct wrapper_buffer *wb = wi ? get_wrapper_buffer_from_handle(device, pInfo->srcBuffer) : NULL;

   if (!wi || !wb || !is_emulated_bcn(device->physical, wi->info.format)) {
      device->banner_bcn->CmdCopyBufferToImage2(commandBuffer, pInfo);
      return;
   }

   VkBufferImageCopy *regions = malloc(sizeof(*regions) * (pInfo->regionCount ? pInfo->regionCount : 1));
   if (!regions)
      return;
   for (uint32_t i = 0; i < pInfo->regionCount; i++) {
      const VkBufferImageCopy2 *r = &pInfo->pRegions[i];
      regions[i] = (VkBufferImageCopy) {
         .bufferOffset = r->bufferOffset,
         .bufferRowLength = r->bufferRowLength,
         .bufferImageHeight = r->bufferImageHeight,
         .imageSubresource = r->imageSubresource,
         .imageOffset = r->imageOffset,
         .imageExtent = r->imageExtent,
      };
   }
   banner_bcn_upload(wcb, wi, wb, pInfo->dstImage, pInfo->dstImageLayout, pInfo->regionCount, regions);
   free(regions);
}

/* The size of the image the wrapper will really create (the unpacked format), as vkCreateImage does. */
static VKAPI_ATTR void VKAPI_CALL
banner_GetDeviceImageMemoryRequirements(VkDevice _device, const VkDeviceImageMemoryRequirements *pInfo,
                                        VkMemoryRequirements2 *pMemoryRequirements)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);
   struct banner_bcn_state *st = device->banner_bcn;

   if (!pInfo || !pInfo->pCreateInfo || !is_emulated_bcn(device->physical, pInfo->pCreateInfo->format)) {
      st->GetDeviceImageMemoryRequirements(_device, pInfo, pMemoryRequirements);
      return;
   }

   VkImageCreateInfo create_info = *pInfo->pCreateInfo;
   VkDeviceImageMemoryRequirements info = *pInfo;
   VkImageFormatListCreateInfo *list = (VkImageFormatListCreateInfo *)
      vk_find_struct_const(create_info.pNext, IMAGE_FORMAT_LIST_CREATE_INFO);
   const uint32_t list_count = list ? list->viewFormatCount : 0;
   const VkFormat *list_formats = list ? list->pViewFormats : NULL;
   static bool said;

   create_info.format = get_format_for_bcn(create_info.format);
   if (create_info.flags & VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT)
      create_info.flags &= ~VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT;
   patch_image_create_info((VkBaseInStructure *)&create_info, &create_info.format);
   info.pCreateInfo = &create_info;
   st->GetDeviceImageMemoryRequirements(_device, &info, pMemoryRequirements);
   if (list) { /* the game's own format list back as it was */
      list->viewFormatCount = list_count;
      list->pViewFormats = list_formats;
   }

   if (!said) {
      said = true;
      fprintf(stderr, "wrapper-bcn: memory requirements answered for the unpacked image (%ux%u, format %d -> %d: %llu bytes)\n",
              create_info.extent.width, create_info.extent.height, (int)pInfo->pCreateInfo->format,
              (int)create_info.format,
              (unsigned long long)pMemoryRequirements->memoryRequirements.size);
   }
}

static void
banner_bcn_install(struct wrapper_device *device)
{
   struct vk_device_dispatch_table *t = &device->vk.dispatch_table;
   struct banner_bcn_state *st;

   if (device->physical->emulate_bcn < 2)
      return;
   st = calloc(1, sizeof(*st));
   if (!st)
      return;
   simple_mtx_init(&st->mutex, mtx_plain);
   st->maps = _mesa_hash_table_u64_create(NULL);
   if (!st->maps) {
      free(st);
      return;
   }
   device->banner_bcn = st;

#define BANNER_HOOK(name) do { st->name = t->name; if (st->name) t->name = banner_##name; } while (0)
   BANNER_HOOK(MapMemory);
   BANNER_HOOK(MapMemory2);
   BANNER_HOOK(UnmapMemory);
   BANNER_HOOK(UnmapMemory2);
   BANNER_HOOK(FreeMemory);
   BANNER_HOOK(BeginCommandBuffer);
   BANNER_HOOK(ResetCommandBuffer);
   BANNER_HOOK(ResetCommandPool);
   BANNER_HOOK(FreeCommandBuffers);
   BANNER_HOOK(DestroyCommandPool);
   BANNER_HOOK(CmdCopyBufferToImage2);
   BANNER_HOOK(GetDeviceImageMemoryRequirements);
   BANNER_HOOK(BindBufferMemory2);
#undef BANNER_HOOK

   fprintf(stderr, "wrapper-bcn: BCn emulation fixes on (driver has no BCn: textures unpacked on the CPU)\n");
}
'''

# 6. "auto" leaves BCn to an Arm driver that reports textureCompressionBC itself (base_supported_features
# is the driver's own, copied before the wrapper turns the feature on).
PDEV = "src/vulkan/wrapper/wrapper_physical_device.c"
patch(PDEV,
      "         } else if (pdevice->driver_properties.driverID == VK_DRIVER_ID_MESA_TURNIP) {\n"
      "            wrapper_emulate_bcn = 0;\n"
      "         }\n"
      "      }\n",
      "         } else if (pdevice->driver_properties.driverID == VK_DRIVER_ID_MESA_TURNIP) {\n"
      "            wrapper_emulate_bcn = 0;\n"
      "         } else if (pdevice->driver_properties.driverID == VK_DRIVER_ID_ARM_PROPRIETARY &&\n"
      "                    pdevice->base_supported_features.textureCompressionBC) {\n"
      "            /* Bannerlator (banner/mali/banner_mali_bcn.py): this Arm driver has BCn itself (a BCn shim):\n"
      "             * the textures go to it as they are. WRAPPER_EMULATE_BCN=2 still forces emulation. */\n"
      "            static bool banner_said_native_bcn;\n"
      "            wrapper_emulate_bcn = 0;\n"
      "            if (!banner_said_native_bcn) {\n"
      "               banner_said_native_bcn = true;\n"
      "               fprintf(stderr, \"wrapper-bcn: the driver has BCn itself (%s %s): BCn emulation off \"\n"
      "                       \"(WRAPPER_EMULATE_BCN=2 forces it on)\\n\",\n"
      "                       pdevice->driver_properties.driverName, pdevice->driver_properties.driverInfo);\n"
      "            }\n"
      "         }\n"
      "      }\n")
s = open(os.path.join(root, PDEV)).read()
if "#include <stdio.h>" not in s:
    open(os.path.join(root, PDEV), "w").write("#include <stdio.h>\n" + s)

p = os.path.join(root, DEV)
s = open(p).read()
assert "banner_bcn_install(struct wrapper_device *device)\n{" not in s
open(p, "w").write(s + CODE)
print("BCn emulation fixes (CopyBufferToImage2, memory requirements, BindBufferMemory2, game mappings, staging per command buffer; native BCn on the Arm driver used when it has it) applied")
