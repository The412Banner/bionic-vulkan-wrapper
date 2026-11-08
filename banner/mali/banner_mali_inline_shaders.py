#!/usr/bin/env python3
# Bannerlator: inline pipeline shaders through the Mali SPIR-V fixups (Pipetto wrapper-25 tree).
#
# wrapper_CreateShaderModule rewrites SPIR-V for the Arm Mali driver (OpConstantComposite ->
# OpSpecConstantComposite, ClipDistance / CullDistance removed). A pipeline can instead carry its shader
# inline: module = VK_NULL_HANDLE plus a VkShaderModuleCreateInfo in the stage's pNext chain
# (maintenance5 / graphics pipeline library; current DXVK hands every shader over this way). Those
# shaders never reached the rewrite. Here each inline stage of vkCreateGraphicsPipelines /
# vkCreateComputePipelines becomes a shader module made by wrapper_CreateShaderModule, so both paths
# get the same SPIR-V; the modules are destroyed once the pipelines exist. Only on the Arm Mali driver,
# the one driver the rewrite runs on; on every other driver both calls go straight to the driver.
# Off switch: BANNER_WRAPPER_INLINE_SHADERS=0.
#
# Same approach as GameNative/mesa PR #35 by Utkarsh Dalal ("wrapper: run inline pipeline shaders
# through the Mali SPIR-V fixups", 0fc46418); written for this tree, not copied.
#
# Also: wrapper_CreateShaderModule never freed its patched copy of the SPIR-V (one leak per shader
# module on Mali, and every inline stage now goes through it), so the copy is freed after the call.
import os
import sys

root = sys.argv[1]
rel = "src/vulkan/wrapper/wrapper_device.c"


def patch(old, new):
    p = os.path.join(root, rel)
    s = open(p).read()
    assert s.count(old) == 1, (rel, old[:70], s.count(old))
    open(p, "w").write(s.replace(old, new))


patch('#include "banner_mali.h"\n', '#include "banner_mali.h"\n#include <stdio.h>\n')

RETURN = ("   return device->dispatch_table.CreateShaderModule(\n"
          "      device->dispatch_handle, &create_info, pAllocator, pShaderModule);\n"
          "}")

NEW = r'''   VkResult banner_result = device->dispatch_table.CreateShaderModule(
      device->dispatch_handle, &create_info, pAllocator, pShaderModule);
   /* Bannerlator: free the patched SPIR-V copy made above for the Arm Mali driver. */
   if (create_info.pCode != pCreateInfo->pCode)
      free((void *)create_info.pCode);
   return banner_result;
}

/* Bannerlator: inline pipeline shaders through the Mali SPIR-V fixups above
 * (banner/mali/banner_mali_inline_shaders.py). */
static bool
banner_inline_fixups_on(const struct wrapper_device *device)
{
   static int on = -1;

   if (device->physical->driver_properties.driverID != VK_DRIVER_ID_ARM_PROPRIETARY)
      return false;
   if (on == -1) {
      const char *sw = getenv("BANNER_WRAPPER_INLINE_SHADERS");
      const char *a = getenv("WRAPPER_NO_PATCH_OPCONSTCOMP");
      const char *b = getenv("WRAPPER_NO_REMOVE_CLIP_DISTANCE");
      bool fixups = !(a && atoi(a)) || !(b && atoi(b));
      on = fixups && !(sw && strcmp(sw, "0") == 0);
      fprintf(stderr, "wrapper-mali: inline pipeline shaders through the SPIR-V fixups: %s\n",
              on ? "on" : (fixups ? "off (BANNER_WRAPPER_INLINE_SHADERS=0)" : "off (both fixups off)"));
   }
   return on;
}

static const VkBaseInStructure *
banner_find_pnext(const void *chain, VkStructureType type)
{
   for (const VkBaseInStructure *p = chain; p; p = p->pNext) {
      if (p->sType == type)
         return p;
   }
   return NULL;
}

static bool
banner_stage_is_inline(const VkPipelineShaderStageCreateInfo *stage)
{
   return stage->module == VK_NULL_HANDLE &&
          banner_find_pnext(stage->pNext, VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO);
}

/* pStages is ignored for a graphics pipeline library without either shader subset. */
static bool
banner_gfx_has_stages(const VkGraphicsPipelineCreateInfo *info)
{
   const VkGraphicsPipelineLibraryCreateInfoEXT *lib = (const void *)
      banner_find_pnext(info->pNext, VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_LIBRARY_CREATE_INFO_EXT);

   if (!info->stageCount || !info->pStages)
      return false;
   return !lib || (lib->flags & (VK_GRAPHICS_PIPELINE_LIBRARY_PRE_RASTERIZATION_SHADERS_BIT_EXT |
                                 VK_GRAPHICS_PIPELINE_LIBRARY_FRAGMENT_SHADER_BIT_EXT));
}

/* Structures a stage's pNext chain may hold ahead of its VkShaderModuleCreateInfo; 0 = unknown. */
static size_t
banner_stage_pnext_size(VkStructureType type)
{
   switch (type) {
   case VK_STRUCTURE_TYPE_DEBUG_UTILS_OBJECT_NAME_INFO_EXT:
      return sizeof(VkDebugUtilsObjectNameInfoEXT);
   case VK_STRUCTURE_TYPE_PIPELINE_ROBUSTNESS_CREATE_INFO_EXT:
      return sizeof(VkPipelineRobustnessCreateInfoEXT);
   case VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_REQUIRED_SUBGROUP_SIZE_CREATE_INFO:
      return sizeof(VkPipelineShaderStageRequiredSubgroupSizeCreateInfo);
   default:
      return 0;
   }
}

/* What one pipeline call made: shader modules to destroy and copies to free afterwards. */
struct banner_inline_scratch {
   VkShaderModule *modules;
   uint32_t module_count, module_cap;
   void **blocks;
   uint32_t block_count, block_cap;
};

static void *
banner_inline_alloc(struct banner_inline_scratch *s, size_t size)
{
   void *p = malloc(size ? size : 1);

   if (!p)
      return NULL;
   if (s->block_count == s->block_cap) {
      uint32_t cap = s->block_cap ? s->block_cap * 2 : 16;
      void **blocks = realloc(s->blocks, cap * sizeof(*blocks));
      if (!blocks) {
         free(p);
         return NULL;
      }
      s->blocks = blocks;
      s->block_cap = cap;
   }
   s->blocks[s->block_count++] = p;
   return p;
}

static bool
banner_inline_keep_module(struct banner_inline_scratch *s, VkShaderModule module)
{
   if (s->module_count == s->module_cap) {
      uint32_t cap = s->module_cap ? s->module_cap * 2 : 8;
      VkShaderModule *modules = realloc(s->modules, cap * sizeof(*modules));
      if (!modules)
         return false;
      s->modules = modules;
      s->module_cap = cap;
   }
   s->modules[s->module_count++] = module;
   return true;
}

static void
banner_inline_finish(struct wrapper_device *device, struct banner_inline_scratch *s)
{
   for (uint32_t i = 0; i < s->module_count; i++)
      device->dispatch_table.DestroyShaderModule(device->dispatch_handle, s->modules[i], NULL);
   for (uint32_t i = 0; i < s->block_count; i++)
      free(s->blocks[i]);
   free(s->modules);
   free(s->blocks);
}

/* An inline stage gets a module from wrapper_CreateShaderModule (the fixups) and loses its
 * VkShaderModuleCreateInfo; the structures ahead of it in the chain are copied to unlink it. */
static VkResult
banner_lower_inline_stage(struct wrapper_device *device, VkPipelineShaderStageCreateInfo *stage,
                          struct banner_inline_scratch *s)
{
   const VkBaseInStructure *ahead[8];
   const VkBaseInStructure *module_info = NULL;
   uint32_t ahead_count = 0;
   bool copyable = true;

   if (stage->module != VK_NULL_HANDLE)
      return VK_SUCCESS;
   for (const VkBaseInStructure *p = stage->pNext; p; p = p->pNext) {
      if (p->sType == VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO) {
         module_info = p;
         break;
      }
      if (ahead_count < ARRAY_SIZE(ahead) && banner_stage_pnext_size(p->sType))
         ahead[ahead_count++] = p;
      else
         copyable = false;
   }
   if (!module_info)
      return VK_SUCCESS;

   VkShaderModuleCreateInfo info = *(const VkShaderModuleCreateInfo *)module_info;
   info.pNext = NULL;
   VkShaderModule module = VK_NULL_HANDLE;
   VkResult result = wrapper_CreateShaderModule(wrapper_device_to_handle(device), &info, NULL, &module);
   if (result != VK_SUCCESS)
      return result;
   if (!banner_inline_keep_module(s, module)) {
      device->dispatch_table.DestroyShaderModule(device->dispatch_handle, module, NULL);
      return VK_ERROR_OUT_OF_HOST_MEMORY;
   }
   stage->module = module;

   if (!copyable) {
      static bool said;
      if (!said) {
         said = true;
         fprintf(stderr, "wrapper-mali: inline shader info left in its pNext chain (unknown structure ahead of it)\n");
      }
      return VK_SUCCESS;
   }
   const void *next = module_info->pNext;
   for (uint32_t i = ahead_count; i-- > 0;) {
      size_t size = banner_stage_pnext_size(ahead[i]->sType);
      VkBaseOutStructure *copy = banner_inline_alloc(s, size);
      if (!copy)
         return VK_ERROR_OUT_OF_HOST_MEMORY;
      memcpy(copy, ahead[i], size);
      copy->pNext = (VkBaseOutStructure *)next;
      next = copy;
   }
   stage->pNext = next;
   return VK_SUCCESS;
}

static void
banner_inline_said_first(void)
{
   static bool said;

   if (!said) {
      said = true;
      fprintf(stderr, "wrapper-mali: inline pipeline shaders lowered to shader modules (first pipeline)\n");
   }
}

VKAPI_ATTR VkResult VKAPI_CALL
wrapper_CreateGraphicsPipelines(VkDevice _device, VkPipelineCache pipelineCache,
                                uint32_t createInfoCount,
                                const VkGraphicsPipelineCreateInfo *pCreateInfos,
                                const VkAllocationCallbacks *pAllocator,
                                VkPipeline *pPipelines)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);
   bool any = false;

   if (banner_inline_fixups_on(device)) {
      for (uint32_t i = 0; i < createInfoCount && !any; i++) {
         if (!banner_gfx_has_stages(&pCreateInfos[i]))
            continue;
         for (uint32_t j = 0; j < pCreateInfos[i].stageCount && !any; j++)
            any = banner_stage_is_inline(&pCreateInfos[i].pStages[j]);
      }
   }
   if (!any)
      return device->dispatch_table.CreateGraphicsPipelines(device->dispatch_handle,
         pipelineCache, createInfoCount, pCreateInfos, pAllocator, pPipelines);

   struct banner_inline_scratch s = {0};
   VkResult result = VK_SUCCESS;
   VkGraphicsPipelineCreateInfo *infos = banner_inline_alloc(&s, createInfoCount * sizeof(*infos));

   if (!infos)
      result = VK_ERROR_OUT_OF_HOST_MEMORY;
   else
      memcpy(infos, pCreateInfos, createInfoCount * sizeof(*infos));
   for (uint32_t i = 0; result == VK_SUCCESS && i < createInfoCount; i++) {
      if (!banner_gfx_has_stages(&infos[i]))
         continue;
      VkPipelineShaderStageCreateInfo *stages =
         banner_inline_alloc(&s, infos[i].stageCount * sizeof(*stages));
      if (!stages) {
         result = VK_ERROR_OUT_OF_HOST_MEMORY;
         break;
      }
      memcpy(stages, infos[i].pStages, infos[i].stageCount * sizeof(*stages));
      for (uint32_t j = 0; result == VK_SUCCESS && j < infos[i].stageCount; j++)
         result = banner_lower_inline_stage(device, &stages[j], &s);
      infos[i].pStages = stages;
   }

   if (result == VK_SUCCESS) {
      banner_inline_said_first();
      result = device->dispatch_table.CreateGraphicsPipelines(device->dispatch_handle,
         pipelineCache, createInfoCount, infos, pAllocator, pPipelines);
   } else {
      for (uint32_t i = 0; i < createInfoCount; i++)
         pPipelines[i] = VK_NULL_HANDLE;
   }
   banner_inline_finish(device, &s);
   return result;
}

VKAPI_ATTR VkResult VKAPI_CALL
wrapper_CreateComputePipelines(VkDevice _device, VkPipelineCache pipelineCache,
                               uint32_t createInfoCount,
                               const VkComputePipelineCreateInfo *pCreateInfos,
                               const VkAllocationCallbacks *pAllocator,
                               VkPipeline *pPipelines)
{
   VK_FROM_HANDLE(wrapper_device, device, _device);
   bool any = false;

   if (banner_inline_fixups_on(device)) {
      for (uint32_t i = 0; i < createInfoCount && !any; i++)
         any = banner_stage_is_inline(&pCreateInfos[i].stage);
   }
   if (!any)
      return device->dispatch_table.CreateComputePipelines(device->dispatch_handle,
         pipelineCache, createInfoCount, pCreateInfos, pAllocator, pPipelines);

   struct banner_inline_scratch s = {0};
   VkResult result = VK_SUCCESS;
   VkComputePipelineCreateInfo *infos = banner_inline_alloc(&s, createInfoCount * sizeof(*infos));

   if (!infos)
      result = VK_ERROR_OUT_OF_HOST_MEMORY;
   else
      memcpy(infos, pCreateInfos, createInfoCount * sizeof(*infos));
   for (uint32_t i = 0; result == VK_SUCCESS && i < createInfoCount; i++)
      result = banner_lower_inline_stage(device, &infos[i].stage, &s);

   if (result == VK_SUCCESS) {
      banner_inline_said_first();
      result = device->dispatch_table.CreateComputePipelines(device->dispatch_handle,
         pipelineCache, createInfoCount, infos, pAllocator, pPipelines);
   } else {
      for (uint32_t i = 0; i < createInfoCount; i++)
         pPipelines[i] = VK_NULL_HANDLE;
   }
   banner_inline_finish(device, &s);
   return result;
}'''

patch(RETURN, NEW)
print("pipetto: inline pipeline shaders through the Mali SPIR-V fixups (BANNER_WRAPPER_INLINE_SHADERS) "
      "+ shader-module SPIR-V copy freed, applied")
