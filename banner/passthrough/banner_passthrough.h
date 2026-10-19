/*
 * Bannerlator: pass-through dispatch for the bionic Vulkan wrapper (Pipetto wrapper-25 lineage).
 *
 * Why. The wrapper wraps every dispatchable handle (VkInstance / VkPhysicalDevice / VkDevice / VkQueue /
 * VkCommandBuffer) in a struct of its own and answers vkGetDeviceProcAddr with a generated trampoline for
 * every device entry point: unwrap the first argument, call the driver's function on the driver's handle.
 * For a vkCmd* call that is one extra indirect call and two dependent loads per command, hundreds to
 * thousands of times per frame; under FEX (x86 game code on arm64) each hop is also an extra translated
 * basic block and an indirect-branch lookup. The wrapper needs none of that for command buffers: its own
 * job is instance / physical-device shaping (extensions, features, "Wrapper(%s)" name), the Mesa WSI
 * (surfaces, swapchains, present), placed memory mapping, shader patching and BCn emulation.
 *
 * What. When pass-through is on for a device (BANNER_WRAPPER_PASSTHROUGH != 0 and no BCn emulation on
 * that device), the application receives the DRIVER's VkCommandBuffer handles and vkGetDeviceProcAddr
 * returns the DRIVER's function pointer for every VkCommandBuffer entry point, so vkCmdDraw & co. go
 * straight to the driver. VkDevice and VkQueue stay wrapped: the Mesa runtime and WSI need them to be
 * vk_device / vk_queue objects, and the Android loader underneath keeps its own data in the driver's
 * VkDevice (vkDestroyDevice / vkGetDeviceQueue / vkAllocateCommandBuffers read it), so those handles
 * cannot be shared with the loader above us. Their entry points keep the one-hop trampoline.
 *
 * Side table. Code that reaches a command buffer WITHOUT knowing its device -- the Mesa WSI's blit
 * path (function pointers taken through wsi_device_init's proc_addr) and vkGetInstanceProcAddr for a
 * device-level name -- gets generated trampolines (banner_pt_command_buffer_trampolines, in
 * wrapper_trampolines.c) that look the device up from the driver handle in a small hash table filled by
 * vkAllocateCommandBuffers and emptied by vkFreeCommandBuffers / vkDestroyCommandPool / vkDestroyDevice,
 * falling back to the wrapped-handle path for a device that stays fully wrapped.
 *
 * Off switch: BANNER_WRAPPER_PASSTHROUGH=0 restores full wrapping (the old dispatch, unchanged).
 * One stderr line per device: "wrapper-dispatch: pass-through (...)" or "wrapper-dispatch: full wrapping (...)".
 */
#ifndef BANNER_PASSTHROUGH_H
#define BANNER_PASSTHROUGH_H

#include "wrapper_private.h"

#ifdef __cplusplus
extern "C" {
#endif

/* BANNER_WRAPPER_PASSTHROUGH != "0" (read once). */
bool banner_pt_enabled(void);

/* Decide pass-through for a freshly created device (its driver dispatch table must be loaded) and log
 * the one wrapper-dispatch line. Sets device->banner_passthrough. */
void banner_pt_device_init(struct wrapper_device *device);

/* vkGetDeviceProcAddr: the wrapper's own pointer, or the driver's for a VkCommandBuffer entry point on
 * a pass-through device. */
PFN_vkVoidFunction banner_pt_get_device_proc_addr(struct wrapper_device *device, const char *name);

/* vkGetInstanceProcAddr / the WSI's proc_addr: func as resolved by the Mesa runtime, except that a
 * VkCommandBuffer entry point becomes the side-table trampoline while pass-through is enabled. */
PFN_vkVoidFunction banner_pt_instance_proc_addr(PFN_vkVoidFunction func, const char *name);

/* The device a driver command buffer belongs to (pass-through devices only), else NULL. */
struct wrapper_device *banner_pt_device_for_cmd(VkCommandBuffer cmd);

void banner_pt_track_cmds(struct wrapper_device *device, VkCommandPool pool,
                          uint32_t count, const VkCommandBuffer *cmds);
void banner_pt_untrack_cmds(uint32_t count, const VkCommandBuffer *cmds);
void banner_pt_untrack_pool(struct wrapper_device *device, VkCommandPool pool);
void banner_pt_untrack_device(struct wrapper_device *device);

/* Generated into wrapper_trampolines.c: one trampoline per VkCommandBuffer entry point, dispatching
 * through banner_pt_device_for_cmd() or, failing that, the wrapped command buffer. */
extern struct vk_device_entrypoint_table banner_pt_command_buffer_trampolines;

#ifdef __cplusplus
}
#endif

#endif /* BANNER_PASSTHROUGH_H */
