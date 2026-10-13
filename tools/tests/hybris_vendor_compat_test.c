#include "vendor_compat.h"
#include "../common/vendor_compat.h"
#include "../layer/layer.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *physical_name = "Samsung Xclipse 540";
static int native_bc, bc_mask = 0xffff, ycbcr_feature = 1;
static uint64_t next_sampler = 0x100;
static VkSampler driver_ycbcr;
static VkSampler driver_layout_sampler;
static int ext_domain_calls, ext_timestamp_calls;
static int allocations, frees, fail_allocation = -1;
static VkInstance instance = (VkInstance)0x10;
static VkPhysicalDevice physical = (VkPhysicalDevice)0x20;
static VkDevice device = (VkDevice)0x30;
static int bionic_dlopen_calls;
static char **bionic_environ;

void *hybris_dlopen(const char *name, int flags)
{
    assert(!strcmp(name, "libc.so"));
    ++bionic_dlopen_calls;
    return (void *)0x50;
}
void *hybris_dlsym(void *handle, const char *name)
{
    assert(handle == (void *)0x50 && !strcmp(name, "environ"));
    return &bionic_environ;
}

static void VKAPI_CALL properties(VkPhysicalDevice handle, VkPhysicalDeviceProperties *out)
{
    memset(out, 0, sizeof(*out));
    snprintf(out->deviceName, sizeof(out->deviceName), "%s", physical_name);
}
static void VKAPI_CALL features(VkPhysicalDevice handle, VkPhysicalDeviceFeatures *out)
{
    memset(out, 0, sizeof(*out));
    out->textureCompressionBC = native_bc;
}
static void VKAPI_CALL features2(VkPhysicalDevice handle, VkPhysicalDeviceFeatures2 *out)
{
    features(handle, &out->features);
    for (VkBaseOutStructure *node = out->pNext; node; node = node->pNext)
        if (node->sType == VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SAMPLER_YCBCR_CONVERSION_FEATURES)
            ((VkPhysicalDeviceSamplerYcbcrConversionFeatures *)node)->samplerYcbcrConversion = ycbcr_feature;
}
static VkResult VKAPI_CALL domains_ext(VkPhysicalDevice handle, uint32_t *count,
    VkTimeDomainEXT *domains)
{
    ++ext_domain_calls;
    if (domains) domains[0] = VK_TIME_DOMAIN_DEVICE_EXT;
    *count = 1;
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL timestamps_ext(VkDevice handle, uint32_t count,
    const VkCalibratedTimestampInfoEXT *infos, uint64_t *timestamps, uint64_t *deviation)
{
    ++ext_timestamp_calls;
    for (uint32_t i = 0; i < count; ++i) timestamps[i] = 42;
    *deviation = 1;
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL create_sampler(VkDevice handle, const VkSamplerCreateInfo *info,
    const VkAllocationCallbacks *allocator, VkSampler *out)
{
    *out = (VkSampler)(uintptr_t)next_sampler++;
    for (const VkBaseInStructure *node = info->pNext; node; node = node->pNext)
        if (node->sType == VK_STRUCTURE_TYPE_SAMPLER_YCBCR_CONVERSION_INFO)
            driver_ycbcr = *out;
    return VK_SUCCESS;
}
static void VKAPI_CALL destroy_sampler(VkDevice handle, VkSampler sampler,
    const VkAllocationCallbacks *allocator)
{
    if (driver_ycbcr == sampler) driver_ycbcr = VK_NULL_HANDLE;
}
static VkResult VKAPI_CALL create_layout(VkDevice handle,
    const VkDescriptorSetLayoutCreateInfo *info, const VkAllocationCallbacks *allocator,
    VkDescriptorSetLayout *out)
{
    driver_layout_sampler = info->pBindings[0].pImmutableSamplers[0];
    if ((info->pBindings[0].stageFlags & VK_SHADER_STAGE_COMPUTE_BIT) &&
        driver_layout_sampler == driver_ycbcr) return VK_ERROR_UNKNOWN;
    *out = (VkDescriptorSetLayout)0x40;
    return VK_SUCCESS;
}

#define PROC(value, symbol) if (!strcmp(name, value)) return (PFN_vkVoidFunction)symbol
static PFN_vkVoidFunction VKAPI_CALL instance_resolver(VkInstance handle, const char *name)
{
    PROC("vkGetPhysicalDeviceProperties", properties);
    PROC("vkGetPhysicalDeviceFeatures", features);
    PROC("vkGetPhysicalDeviceFeatures2", features2);
    PROC("vkGetPhysicalDeviceCalibrateableTimeDomainsEXT", domains_ext);
    PROC("vkGetCalibratedTimestampsEXT", timestamps_ext);
    return NULL;
}
static PFN_vkVoidFunction VKAPI_CALL device_resolver(VkDevice handle, const char *name)
{
    PROC("vkCreateSampler", create_sampler);
    PROC("vkDestroySampler", destroy_sampler);
    PROC("vkCreateDescriptorSetLayout", create_layout);
    PROC("vkGetCalibratedTimestampsEXT", timestamps_ext);
    return NULL;
}

int hybris_layer_lookup_physical(VkPhysicalDevice handle, struct hybris_layer_physical *out)
{
    if (handle != physical) return 0;
    *out = (struct hybris_layer_physical){.instance = instance, .resolver = instance_resolver,
        .api_version = VK_API_VERSION_1_3};
    return 1;
}
int hybris_layer_lookup_device(VkDevice handle, struct hybris_layer_device *out)
{
    if (handle != device) return 0;
    *out = (struct hybris_layer_device){.handle = device, .physical = physical,
        .resolver = device_resolver};
    return 1;
}
unsigned hybris_bc_physical_mask(VkPhysicalDevice handle) { return bc_mask; }
void *hybris_scaled_alloc(const VkAllocationCallbacks *allocator, size_t size,
    VkSystemAllocationScope scope)
{
    return allocator ? allocator->pfnAllocation(allocator->pUserData, size,
        _Alignof(max_align_t), scope) : calloc(1, size);
}
void hybris_scaled_free(const VkAllocationCallbacks *allocator, void *memory)
{
    if (!memory) return;
    if (allocator) allocator->pfnFree(allocator->pUserData, memory);
    else free(memory);
}
static void *VKAPI_PTR test_allocate(void *data, size_t size, size_t alignment,
    VkSystemAllocationScope scope)
{
    if (fail_allocation == allocations) return NULL;
    ++allocations;
    return calloc(1, size);
}
static void VKAPI_PTR test_free(void *data, void *memory)
{
    if (memory) { ++frees; free(memory); }
}

static void reset(const char *name)
{
    physical_name = name;
    native_bc = 0;
    bc_mask = 0xffff;
    ycbcr_feature = 1;
    next_sampler = 0x100;
    driver_ycbcr = driver_layout_sampler = VK_NULL_HANDLE;
    ext_domain_calls = ext_timestamp_calls = 0;
}

int main(void)
{
    setenv("HYBRIS_VENDOR_TEST", "copied", 1);
    assert(hybris_vendor_compat_prepare_vulkan_hal("/vendor/lib64/hw/vulkan.mali.so"));
    assert(hybris_vendor_compat_prepare_vulkan_hal("/vendor/lib64/hw/vulkan.samsung.so.bak"));
    assert(hybris_vendor_compat_prepare_vulkan_hal("/vendor/vulkan.samsung.so/not-a-hal"));
    assert(!bionic_dlopen_calls && !bionic_environ);
    assert(hybris_vendor_compat_prepare_vulkan_hal("/vendor/lib64/hw/vulkan.samsung.so"));
    assert(bionic_dlopen_calls == 1 && bionic_environ);
    int copied = 0;
    for (char **entry = bionic_environ; *entry; ++entry)
        if (!strcmp(*entry, "HYBRIS_VENDOR_TEST=copied")) copied = 1;
    assert(copied);

    unsetenv("HYBRIS_VULKAN_HAL");
    reset("Samsung Xclipse 540");
    assert(!hybris_vendor_xclipse_physical(physical));
    assert(!hybris_vendor_physical_proc("vkGetPhysicalDeviceCalibrateableTimeDomainsKHR"));
    setenv("HYBRIS_VULKAN_HAL", "/vendor/lib64/hw/vulkan.samsung.so.bak", 1);
    assert(!hybris_vendor_xclipse_physical(physical));
    assert(!hybris_vendor_physical_alias_proc(instance, instance_resolver,
        "vkGetPhysicalDeviceCalibrateableTimeDomainsKHR"));

    setenv("HYBRIS_VULKAN_HAL", "/vendor/lib64/hw/vulkan.samsung.so", 1);
    reset("Mali-G715");
    assert(!hybris_vendor_xclipse_physical(physical));
    assert(!hybris_vendor_xclipse_bc(physical));
    VkAllocationCallbacks allocator = {.pfnAllocation = test_allocate, .pfnFree = test_free};
    assert(hybris_vendor_attach_device(device, physical, device_resolver, &allocator) == VK_SUCCESS);

    reset("PowerVR B-Series BXM-4-64");
    assert(!hybris_vendor_xclipse_physical(physical));
    VkFormatProperties format = {.optimalTilingFeatures = VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT};
    hybris_vendor_adjust_bc_format(physical, VK_FORMAT_BC4_UNORM_BLOCK, &format);
    assert(!(format.optimalTilingFeatures & VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT));

    reset("Xclipse 540");
    assert(hybris_vendor_xclipse_physical(physical));
    assert(hybris_vendor_xclipse_bc(physical));
    native_bc = 1;
    assert(!hybris_vendor_xclipse_bc(physical));
    native_bc = 0;
    bc_mask = 0;
    assert(!hybris_vendor_xclipse_bc(physical));
    bc_mask = 0xffff;
    format.optimalTilingFeatures = VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT;
    hybris_vendor_adjust_bc_format(physical, VK_FORMAT_BC4_UNORM_BLOCK, &format);
    assert(format.optimalTilingFeatures & VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT);
    format.optimalTilingFeatures = VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT;
    hybris_vendor_adjust_bc_format(physical, VK_FORMAT_BC1_RGBA_UNORM_BLOCK, &format);
    assert(!(format.optimalTilingFeatures & VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT));

    assert(hybris_vendor_attach_device(device, physical, device_resolver, NULL) == VK_SUCCESS);
    PFN_vkCreateSampler create = (PFN_vkCreateSampler)hybris_vendor_device_proc("vkCreateSampler");
    PFN_vkCreateDescriptorSetLayout layout = (PFN_vkCreateDescriptorSetLayout)
        hybris_vendor_device_proc("vkCreateDescriptorSetLayout");
    VkSamplerYcbcrConversionInfo conversion = {
        .sType = VK_STRUCTURE_TYPE_SAMPLER_YCBCR_CONVERSION_INFO,
        .conversion = (VkSamplerYcbcrConversion)1};
    VkSamplerCreateInfo sampler_info = {
        .sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO, .pNext = &conversion};
    VkSampler ycbcr;
    assert(create(device, &sampler_info, &allocator, &ycbcr) == VK_SUCCESS);
    VkSampler immutable[] = {ycbcr};
    VkDescriptorSetLayoutBinding binding = {.binding = 4,
        .descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, .descriptorCount = 1,
        .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT, .pImmutableSamplers = immutable};
    VkDescriptorSetLayoutCreateInfo layout_info = {
        .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .bindingCount = 1, .pBindings = &binding};
    VkDescriptorSetLayout set_layout;
    assert(layout(device, &layout_info, &allocator, &set_layout) == VK_SUCCESS);
    assert(driver_layout_sampler != ycbcr && immutable[0] == ycbcr);
    int live = allocations - frees;
    fail_allocation = allocations + 1;
    assert(layout(device, &layout_info, &allocator, &set_layout) == VK_ERROR_OUT_OF_HOST_MEMORY);
    fail_allocation = -1;
    assert(allocations - frees == live);

    PFN_vkGetPhysicalDeviceCalibrateableTimeDomainsKHR query_domains =
        (PFN_vkGetPhysicalDeviceCalibrateableTimeDomainsKHR)
        hybris_vendor_physical_alias_proc(instance, instance_resolver,
            "vkGetPhysicalDeviceCalibrateableTimeDomainsKHR");
    uint32_t count = 0;
    assert(query_domains(physical, &count, NULL) == VK_SUCCESS && count == 1 && ext_domain_calls == 1);
    assert(hybris_vendor_physical_alias_proc(instance, instance_resolver,
        "vkGetCalibratedTimestampsKHR"));
    PFN_vkGetCalibratedTimestampsKHR query_timestamps = (PFN_vkGetCalibratedTimestampsKHR)
        hybris_vendor_device_alias_proc(device, "vkGetCalibratedTimestampsKHR");
    VkCalibratedTimestampInfoKHR stamp = {.sType = VK_STRUCTURE_TYPE_CALIBRATED_TIMESTAMP_INFO_KHR,
        .timeDomain = VK_TIME_DOMAIN_DEVICE_KHR};
    uint64_t timestamp = 0, deviation = 0;
    assert(query_timestamps(device, 1, &stamp, &timestamp, &deviation) == VK_SUCCESS);
    assert(timestamp == 42 && deviation == 1 && ext_timestamp_calls == 1);

    PFN_vkDestroySampler destroy = (PFN_vkDestroySampler)
        hybris_vendor_device_proc("vkDestroySampler");
    destroy(device, ycbcr, &allocator);
    hybris_vendor_release_device(device);
    assert(allocations == frees);
    puts("hybris vendor compatibility ok");
    return 0;
}
